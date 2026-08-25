"""One versioned entry point for calendar freshness, forecast, and warnings."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import logging
import uuid
from typing import Any
from zoneinfo import ZoneInfo

from app.integrations.feishu.calendar import CalendarService
from app.repositories import (
    CalendarSnapshotRepository,
    ForecastSnapshotRepository,
    ObservationRepository,
    ParticipantRepository,
    ProfileRepository,
    LearnedProfileRepository,
    WarningScheduleRepository,
    ForecastInputChangedError,
)
from app.services.event_semantic_preprocessor import EventSemanticPreprocessor
from app.services.care_message_service import (
    CARE_MESSAGE_SCHEMA_VERSION,
    CareMessageService,
)
from app.services.care_context import (
    CARE_CONTEXT_SCHEMA_VERSION,
    CARE_RECENT_OBSERVATION_MAX_AGE_MINUTES,
)
from app.services.care_intervention_policy import CARE_INTERVENTION_POLICY_VERSION
from app.services.care_templates import CARE_TEMPLATE_LIBRARY_VERSION
from app.services.forecast_initial_state import (
    ForecastInitialState,
    ForecastInitialStateResolver,
)
from app.services.prediction_service import PredictionService
from app.services.warning_policy import WarningPolicy
from app.services.profile_calibration import layered_profile
from app.repositories_daily_review import RetrospectiveCurveRepository
from app.repositories_care import ParticipantCarePreferenceRepository
from services.course_catalog import COURSE_CATALOG_REVISION, COURSE_RESOLVER_VERSION
from services.event_lifecycle import EVENT_SCHEMA_VERSION, prepare_event_instances
from services.event_semantic_prompt import PROMPT_VERSION
from services.event_semantics import SEMANTIC_SCHEMA_VERSION
from services.semantic_model_inputs import semantic_model_inputs


logger = logging.getLogger(__name__)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def normalized_calendar_revision(events: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    normalized = []
    for event in events:
        normalized.append({
            "id": str(event.get("id") or event.get("event_id") or ""),
            "summary": str(event.get("summary") or "")[:200],
            "description": str(event.get("description") or "")[:800],
            "start_time": str(event.get("start_time") or ""),
            "end_time": str(event.get("end_time") or ""),
            "event_type": str(event.get("event_type") or ""),
            "task_type": str(event.get("task_type") or ""),
            "metadata": dict(event.get("metadata") or {}),
        })
    normalized.sort(key=lambda row: (row["start_time"], row["end_time"], row["id"], row["summary"]))
    return _sha(normalized), normalized


def classified_calendar_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    classified: list[dict[str, Any]] = []
    for event in events:
        metadata = dict(event.get("metadata") or {})
        classification = dict(metadata.get("classification") or {})
        catalog_context = dict(classification.get("course_catalog_context") or {})
        semantic = dict(metadata.get("semantic") or {})
        classified.append(
            {
                "id": str(event.get("id") or event.get("event_id") or ""),
                "summary": str(event.get("summary") or "")[:200],
                "description": str(event.get("description") or "")[:800],
                "start_time": str(event.get("start_time") or ""),
                "end_time": str(event.get("end_time") or ""),
                "event_type": str(event.get("event_type") or "other"),
                "task_type": str(event.get("task_type") or "general"),
                "course_name": event.get("course_name"),
                "course_code": event.get("course_code"),
                "related_course_name": event.get("related_course_name"),
                "related_course_code": event.get("related_course_code"),
                "course_match_confidence": event.get("course_match_confidence"),
                "course_match_source": event.get("course_match_source"),
                "course_catalog_revision": (
                    event.get("course_catalog_revision")
                    or catalog_context.get("catalog_revision")
                ),
                "course_resolver_version": catalog_context.get("resolver_version"),
                "classification_source": classification.get("source"),
                "classification_confidence": classification.get("confidence"),
                "semantic_source": semantic.get("source"),
            }
        )
    return classified


class ForecastCoordinator:
    def __init__(
        self, *, participants: ParticipantRepository, profiles: ProfileRepository,
        observations: ObservationRepository, calendar: CalendarService,
        calendar_snapshots: CalendarSnapshotRepository,
        semantics: EventSemanticPreprocessor, prediction: PredictionService,
        forecasts: ForecastSnapshotRepository, warnings: WarningScheduleRepository,
        timezone_name: str, materiality_threshold: float = 0.03,
        warning_lead_minutes: int = 20, warning_late_grace_minutes: int = 10,
        warning_episode_drift_minutes: int = 15,
        learned_profiles: LearnedProfileRepository | None = None,
        retrospective_curves: RetrospectiveCurveRepository | None = None,
        care_preferences: ParticipantCarePreferenceRepository | None = None,
    ):
        self.participants = participants
        self.profiles = profiles
        self.observations = observations
        self.calendar = calendar
        self.calendar_snapshots = calendar_snapshots
        self.semantics = semantics
        self.prediction = prediction
        self.forecasts = forecasts
        self.warnings = warnings
        self.timezone = ZoneInfo(timezone_name)
        self.materiality_threshold = materiality_threshold
        self.warning_lead_minutes = warning_lead_minutes
        self.warning_late_grace_minutes = warning_late_grace_minutes
        self.warning_episode_drift_minutes = warning_episode_drift_minutes
        self.warning_policy = WarningPolicy(warnings.delivery_policy)
        self.learned_profiles = learned_profiles
        self.retrospective_curves = retrospective_curves
        self.initial_states = ForecastInitialStateResolver()
        self.care_messages = CareMessageService(timezone_name)
        self.care_preferences = care_preferences
        self._inflight: dict[tuple[uuid.UUID, date], dict[str, Any]] = {}
        self._guard = asyncio.Lock()

    def _warning_revision(self) -> tuple[str, dict[str, object]]:
        payload = {
            **self.warning_policy.identity_payload(),
            "lead_minutes": self.warning_lead_minutes,
            "late_grace_minutes": self.warning_late_grace_minutes,
            "episode_drift_minutes": self.warning_episode_drift_minutes,
            "care_context_schema_version": CARE_CONTEXT_SCHEMA_VERSION,
            "care_recent_observation_max_age_minutes": (
                CARE_RECENT_OBSERVATION_MAX_AGE_MINUTES
            ),
            "care_message_schema_version": CARE_MESSAGE_SCHEMA_VERSION,
            "care_intervention_policy_version": CARE_INTERVENTION_POLICY_VERSION,
            "care_template_library_version": CARE_TEMPLATE_LIBRARY_VERSION,
        }
        return _sha(payload), payload

    def _derive_warning_state(
        self,
        output: dict[str, Any],
        target: date,
        care_inputs: dict[str, Any] | None = None,
    ) -> tuple[
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[dict[str, Any]],
    ]:
        raw_candidates = list(output.get("alerts") or [])
        facts = dict(care_inputs or {})
        preferences = dict(facts.get("care_preferences") or {})
        if self.care_preferences is not None:
            allowed_candidates = []
            for alert in raw_candidates:
                try:
                    hour, minute = (
                        int(part)
                        for part in str(alert.get("time") or "00:00")[:5].split(":")
                    )
                    risk_time = datetime.combine(
                        target, time(hour, minute), self.timezone
                    )
                except (TypeError, ValueError):
                    continue
                scheduled_at = risk_time - timedelta(
                    minutes=self.warning_lead_minutes
                )
                if self.care_preferences.allows_scheduled_at(
                    preferences, scheduled_at
                ):
                    allowed_candidates.append(alert)
            raw_candidates = allowed_candidates
        selected_candidates = self.warning_policy.select_daily_candidates(
            raw_candidates
        )
        if self.care_preferences is not None:
            user_max = int(
                preferences.get(
                    "effective_max_proactive_care_per_day",
                    self.warning_policy.max_daily_sends,
                )
            )
            selected_candidates = selected_candidates[: max(0, user_max)]
        selected = [
            self.care_messages.contextualize_alert(
                alert,
                source="forecast_warning",
                local_date=target,
                calendar_events=list(
                    facts.get("calendar_events")
                    or output.get("classified_calendar_events")
                    or []
                ),
                calendar_degraded=bool(
                    facts.get("calendar_degraded", output.get("calendar_degraded"))
                ),
                recent_observation=facts.get("recent_observation"),
                profile=facts.get("profile"),
                profile_version=facts.get("profile_version"),
                care_preferences=preferences or None,
            )
            for alert in selected_candidates
        ]
        windows = self._warning_windows(selected, target)
        serialized_windows = [
            self._serializable_warning(item) for item in windows
        ]
        return selected, windows, serialized_windows

    @staticmethod
    def _warning_derivatives_match(
        snapshot: dict[str, Any],
        *,
        selected: list[dict[str, Any]],
        windows: list[dict[str, Any]],
        warning_revision: str,
        warning_policy_config: dict[str, object],
    ) -> bool:
        output = dict(snapshot.get("output") or {})
        return (
            _canonical(output.get("selected_warning_candidates"))
            == _canonical(selected)
            and _canonical(snapshot.get("warning_windows"))
            == _canonical(windows)
            and output.get("warning_revision") == warning_revision
            and _canonical(output.get("warning_policy_config"))
            == _canonical(warning_policy_config)
        )

    async def _reconcile_cached_warning_state(
        self,
        snapshot: dict[str, Any],
        target: date,
        *,
        warning_revision: str,
        warning_policy_config: dict[str, object],
        care_inputs: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        output = dict(snapshot.get("output") or {})
        selected, windows, serialized_windows = self._derive_warning_state(
            output, target, care_inputs
        )
        if self._warning_derivatives_match(
            snapshot,
            selected=selected,
            windows=serialized_windows,
            warning_revision=warning_revision,
            warning_policy_config=warning_policy_config,
        ):
            return {**snapshot, "warning_reconciled": False}

        persisted_selected = output.get("selected_warning_candidates")
        persisted_windows = snapshot.get("warning_windows")
        repaired, warning_diff, reconciled = await asyncio.to_thread(
            self.forecasts.reconcile_warning_derivatives,
            self.warnings,
            uuid.UUID(str(snapshot["participant_id"])),
            target,
            forecast_id=uuid.UUID(str(snapshot["id"])),
            forecast_version=str(snapshot["forecast_version"]),
            selected_candidates=selected,
            warning_windows=serialized_windows,
            warning_revision=warning_revision,
            warning_policy_config=warning_policy_config,
            warnings=windows,
            now=datetime.now(timezone.utc),
        )
        if reconciled:
            logger.info(
                "forecast_warning_reconciled",
                extra={
                    "participant_id": str(snapshot["participant_id"]),
                    "local_date": target.isoformat(),
                    "forecast_id": str(snapshot["id"]),
                    "forecast_version_prefix": str(
                        snapshot["forecast_version"]
                    )[:12],
                    "raw_alerts": len(output.get("alerts") or []),
                    "persisted_selected": len(persisted_selected or []),
                    "expected_selected": len(selected),
                    "persisted_windows": len(persisted_windows or []),
                    "expected_windows": len(serialized_windows),
                    "warning_diff": warning_diff,
                },
            )
        return {
            **repaired,
            "warning_reconciled": reconciled,
            "warning_diff": warning_diff,
        }

    async def ensure_forecast(
        self, participant_id: uuid.UUID, local_date: date | str, reason: str,
        *, refresh_calendar: bool = True, enqueue_enrichment: bool = True,
        force_followup: bool = False,
    ) -> dict[str, Any]:
        target = date.fromisoformat(local_date) if isinstance(local_date, str) else local_date
        key = (participant_id, target)
        async with self._guard:
            flight = self._inflight.get(key)
            if flight is None:
                flight = {
                    "generation": 1,
                    "pending_refresh": refresh_calendar,
                    "pending_enrichment": enqueue_enrichment,
                    "active_refresh": False,
                    "active_enrichment": False,
                    "reason": reason,
                }
                task = asyncio.create_task(
                    self._run_flight(key, flight, participant_id, target),
                    name=f"forecast-{participant_id}-{target.isoformat()}",
                )
                flight["task"] = task
                self._inflight[key] = flight
            else:
                refresh_upgrade = bool(
                    refresh_calendar
                    and not flight["pending_refresh"]
                    and not flight["active_refresh"]
                )
                enrichment_upgrade = bool(
                    enqueue_enrichment
                    and not flight["pending_enrichment"]
                    and not flight["active_enrichment"]
                )
                if force_followup or refresh_upgrade or enrichment_upgrade:
                    flight["generation"] += 1
                    flight["pending_refresh"] = bool(
                        flight["pending_refresh"] or refresh_calendar
                    )
                    flight["pending_enrichment"] = bool(
                        flight["pending_enrichment"] or enqueue_enrichment
                    )
                    flight["reason"] = reason
            task = flight["task"]
        return await asyncio.shield(task)

    async def _run_flight(
        self, key: tuple[uuid.UUID, date], flight: dict[str, Any],
        participant_id: uuid.UUID, target: date,
    ) -> dict[str, Any]:
        try:
            while True:
                async with self._guard:
                    generation = int(flight["generation"])
                    refresh_calendar = bool(flight["pending_refresh"])
                    enqueue_enrichment = bool(flight["pending_enrichment"])
                    reason = str(flight["reason"])
                    flight["pending_refresh"] = False
                    flight["pending_enrichment"] = False
                    flight["active_refresh"] = refresh_calendar
                    flight["active_enrichment"] = enqueue_enrichment
                try:
                    result = await self._ensure_once(
                        participant_id,
                        target,
                        reason,
                        refresh_calendar=refresh_calendar,
                        enqueue_enrichment=enqueue_enrichment,
                    )
                except ForecastInputChangedError as exc:
                    # The repository is the final authority. Retry inside the
                    # same flight so callers cannot observe a stale completion.
                    async with self._guard:
                        if int(flight["generation"]) <= generation:
                            flight["generation"] = generation + 1
                        if exc.input_name == "calendar":
                            flight["pending_refresh"] = True
                        flight["pending_enrichment"] = bool(
                            flight["pending_enrichment"] or enqueue_enrichment
                        )
                        flight["reason"] = f"{exc.input_name}_changed_during_forecast"
                        flight["active_refresh"] = False
                        flight["active_enrichment"] = False
                    continue
                except Exception:
                    # A transient failure must not erase an input mutation that
                    # arrived while this generation was running. If a newer
                    # generation is already dirty, consume it before deciding
                    # the shared flight has failed. With no newer work, preserve
                    # the original exception for every joined caller.
                    async with self._guard:
                        flight["active_refresh"] = False
                        flight["active_enrichment"] = False
                        retry_dirty = int(flight["generation"]) > generation
                    if retry_dirty:
                        continue
                    raise
                async with self._guard:
                    flight["active_refresh"] = False
                    flight["active_enrichment"] = False
                    if int(flight["generation"]) > generation:
                        continue
                    if self._inflight.get(key) is flight:
                        self._inflight.pop(key, None)
                    return result
        finally:
            async with self._guard:
                if self._inflight.get(key) is flight:
                    self._inflight.pop(key, None)

    async def _calendar_snapshot(
        self, participant_id: uuid.UUID, target: date, refresh: bool,
    ) -> tuple[dict[str, Any], bool]:
        current = await asyncio.to_thread(self.calendar_snapshots.get, participant_id, target)
        if not refresh and current is not None:
            return current, False
        day_start = datetime.combine(target, time.min, self.timezone)
        try:
            raw_events = await self.calendar.get_events(participant_id, day_start, day_start + timedelta(days=1))
            revision, normalized = normalized_calendar_revision(raw_events)
            return await asyncio.to_thread(
                self.calendar_snapshots.upsert, participant_id, target,
                revision=revision, events=normalized, degraded=False,
            )
        except Exception as exc:
            if current is not None:
                return await asyncio.to_thread(
                    self.calendar_snapshots.upsert, participant_id, target,
                    revision=current["calendar_revision"], events=current["events"],
                    degraded=True, refresh_error_class=type(exc).__name__,
                )
            revision, normalized = normalized_calendar_revision([])
            return await asyncio.to_thread(
                self.calendar_snapshots.upsert, participant_id, target,
                revision=revision, events=normalized, degraded=True,
                refresh_error_class=type(exc).__name__,
            )

    async def _ensure_once(
        self, participant_id: uuid.UUID, target: date, reason: str, *,
        refresh_calendar: bool, enqueue_enrichment: bool,
    ) -> dict[str, Any]:
        profile_row = await asyncio.to_thread(self.profiles.current, participant_id)
        learned_row = (
            await asyncio.to_thread(self.learned_profiles.current, participant_id)
            if self.learned_profiles is not None else None
        )
        effective_profile, profile_layers = layered_profile(profile_row, learned_row)
        initial_state = await self._resolve_initial_state(
            participant_id,
            target,
            refresh_calendar=refresh_calendar,
            effective_profile=effective_profile,
        )
        calendar_snapshot, calendar_changed = await self._calendar_snapshot(
            participant_id, target, refresh_calendar
        )
        participant = await asyncio.to_thread(self.participants.get, participant_id)
        consent = bool(participant and participant.external_llm_consent_at)
        events = prepare_event_instances(calendar_snapshot["events"], target.isoformat())
        semantic_events, semantic_revision, semantic_status, misses = await asyncio.to_thread(
            self.semantics.prepare, participant_id, events, consent=consent
        )
        presentation_events = classified_calendar_events(semantic_events)
        classified_event_revision = _sha(presentation_events)
        classification_facts_available = all(
            item.get("event_type")
            or (item.get("metadata") or {}).get("classification")
            for item in semantic_events
        )
        local_now = datetime.now(self.timezone)
        observation_window_start = None
        observation_window_end = None
        if target == local_now.date():
            observation_window_start = datetime.combine(
                target, time.min, self.timezone
            ).astimezone(timezone.utc)
            observation_window_end = datetime.combine(
                target + timedelta(days=1), time.min, self.timezone
            ).astimezone(timezone.utc)
        observations = (
            await asyncio.to_thread(
                self.observations.for_local_date,
                participant_id,
                target,
                timezone_name=self.timezone.key,
                as_of=local_now,
                limit=100,
            )
            if target == local_now.date()
            else []
        )
        recent_care_observation = None
        try:
            recent_rows = await asyncio.to_thread(
                self.observations.recent_before,
                participant_id,
                before=local_now,
                max_age=timedelta(
                    minutes=CARE_RECENT_OBSERVATION_MAX_AGE_MINUTES
                ),
                limit=1,
            )
            recent_care_observation = recent_rows[0] if recent_rows else None
        except Exception as exc:
            logger.warning(
                "care_recent_observation_unavailable participant_id=%s "
                "error_class=%s",
                participant_id,
                type(exc).__name__,
            )
        care_inputs = {
            "calendar_events": presentation_events,
            "calendar_degraded": bool(calendar_snapshot["degraded"]),
            "recent_observation": recent_care_observation,
            "profile": effective_profile,
            "profile_version": profile_row.get("version") if profile_row else None,
            "care_preferences": (
                await asyncio.to_thread(
                    self.care_preferences.get, participant_id
                )
                if self.care_preferences is not None
                else None
            ),
        }
        observation_revision = _sha(observations)
        profile_revision = _sha({
            "explicit": profile_row,
            "learned": learned_row,
        })
        latest = await asyncio.to_thread(self.forecasts.latest, participant_id, target)
        algorithm_version = str(self.prediction.model.MODEL_VERSION)
        warning_revision, warning_policy_config = self._warning_revision()
        expected_version = _sha({
            "calendar_revision": calendar_snapshot["calendar_revision"],
            "semantic_revision": semantic_revision,
            "classified_event_revision": classified_event_revision,
            "observation_revision": observation_revision,
            "profile_revision": profile_revision,
            "algorithm_version": algorithm_version,
            "warning_revision": warning_revision,
            "initial_state_revision": initial_state.revision,
        })
        if latest and latest["forecast_version"] == expected_version:
            cached = await self._reconcile_cached_warning_state(
                latest,
                target,
                warning_revision=warning_revision,
                warning_policy_config=warning_policy_config,
                care_inputs=care_inputs,
            )
            result = {
                **cached,
                "cache_hit": True,
                "calendar_changed": calendar_changed,
                "reason": reason,
            }
        elif (
            latest
            and latest["calendar_revision"] == calendar_snapshot["calendar_revision"]
            and latest.get("observation_revision", "") == observation_revision
            and latest["algorithm_version"] == algorithm_version
            and (latest.get("output") or {}).get("profile_revision") == profile_revision
            and (latest.get("output") or {}).get("warning_revision") == warning_revision
            and (latest.get("output") or {}).get("initial_state_revision")
            == initial_state.revision
            and (
                not classification_facts_available
                or (latest.get("output") or {}).get("classified_event_revision")
                == classified_event_revision
            )
            and self._semantic_input_delta(latest["semantic_input"], semantic_events)
            < self.materiality_threshold
        ):
            # Persisted forecast remains the effective semantic version until
            # enrichment changes enough to affect the model.  This keeps later
            # curve requests from undoing the materiality decision.
            cached = await self._reconcile_cached_warning_state(
                latest,
                target,
                warning_revision=warning_revision,
                warning_policy_config=warning_policy_config,
                care_inputs=care_inputs,
            )
            result = {
                **cached, "cache_hit": True, "calendar_changed": calendar_changed,
                "material_change": False, "reason": reason,
            }
        else:
            output = await asyncio.to_thread(
                self.prediction.calculate,
                profile=effective_profile,
                observations=observations, calendar_events=semantic_events,
                calendar_degraded=calendar_snapshot["degraded"], local_date=target.isoformat(),
                initial_state=initial_state.model_override,
            )
            output["profile_layers"] = profile_layers
            output["profile_revision"] = profile_revision
            output["warning_revision"] = warning_revision
            output["warning_policy_config"] = warning_policy_config
            output["initial_state"] = initial_state.to_dict()
            output["initial_state_revision"] = initial_state.revision
            output["classified_calendar_events"] = presentation_events
            output["classified_event_revision"] = classified_event_revision
            output["event_schema_version"] = EVENT_SCHEMA_VERSION
            output["semantic_schema_version"] = SEMANTIC_SCHEMA_VERSION
            output["semantic_prompt_version"] = PROMPT_VERSION
            output["course_resolver_version"] = COURSE_RESOLVER_VERSION
            output["course_catalog_revision"] = COURSE_CATALOG_REVISION
            output["care_message_schema_version"] = CARE_MESSAGE_SCHEMA_VERSION
            output["care_intervention_policy_version"] = (
                CARE_INTERVENTION_POLICY_VERSION
            )
            output["care_template_library_version"] = CARE_TEMPLATE_LIBRARY_VERSION
            curve = list(output.get("trajectory") or [])
            peaks = sorted(curve, key=lambda point: float(point.get("stress_0_10") or 0.0), reverse=True)[:5]
            selected_alerts, warning_windows, serialized_windows = (
                self._derive_warning_state(output, target, care_inputs)
            )
            output["selected_warning_candidates"] = selected_alerts
            semantic_input = [
                {"event_id": item.get("id"), "semantic": (item.get("metadata") or {}).get("semantic")}
                for item in semantic_events
            ]
            saved, warning_diff = await asyncio.to_thread(
                self.forecasts.save_and_sync_warnings, self.warnings, participant_id, target,
                calendar_revision=calendar_snapshot["calendar_revision"],
                semantic_revision=semantic_revision, algorithm_version=algorithm_version,
                observation_revision=observation_revision,
                forecast_version=expected_version, semantic_status=semantic_status,
                semantic_input=semantic_input, curve=curve, peaks=peaks,
                warning_windows=serialized_windows,
                output=output, warnings=warning_windows, now=datetime.now(timezone.utc),
                observation_window_start=observation_window_start,
                observation_window_end=observation_window_end,
                verify_current_inputs=True,
            )
            result = {**saved, "cache_hit": False, "calendar_changed": calendar_changed,
                      "warning_reconciled": False, "warning_diff": warning_diff,
                      "reason": reason}
        result.update({
            "calendar_fresh": not calendar_snapshot["degraded"],
            "calendar_stale": bool(calendar_snapshot["degraded"]),
            "calendar_degraded": bool(calendar_snapshot["degraded"]),
            "calendar_last_refresh_success_at": calendar_snapshot.get("last_refresh_success_at"),
            "calendar_last_refresh_error_class": calendar_snapshot.get("last_refresh_error_class"),
            "calendar_events": list(
                (result.get("output") or {}).get("classified_calendar_events")
                or presentation_events
            ),
        })
        if enqueue_enrichment and misses and consent:
            async def recompute() -> None:
                await self.ensure_forecast(
                    participant_id, target, "semantic_enrichment_completion",
                    refresh_calendar=False, enqueue_enrichment=False,
                    force_followup=True,
                )
            await self.semantics.enqueue(
                participant_id, misses, recompute,
                completion_key=(participant_id, target),
            )
        return result

    async def _resolve_initial_state(
        self,
        participant_id: uuid.UUID,
        target: date,
        *,
        refresh_calendar: bool,
        effective_profile: dict[str, Any],
    ) -> ForecastInitialState:
        local_today = datetime.now(self.timezone).date()
        previous = None
        if target == local_today:
            previous = await asyncio.to_thread(
                self.forecasts.latest,
                participant_id,
                target - timedelta(days=1),
            )
        elif target == local_today + timedelta(days=1):
            previous = await self.ensure_forecast(
                participant_id,
                local_today,
                "next_day_initial_state",
                refresh_calendar=refresh_calendar,
                enqueue_enrichment=False,
            )
        terminal_override = None
        if previous is not None and self.retrospective_curves is not None:
            previous_date = target - timedelta(days=1)
            retrospective = await asyncio.to_thread(
                self.retrospective_curves.latest, participant_id, previous_date
            )
            previous_version = str(previous.get("forecast_version") or "")
            retrospective_version = str(
                (retrospective or {}).get("source_forecast_version") or ""
            )
            versions_match = bool(previous_version) and (
                retrospective_version == previous_version
            )
            if retrospective is not None and not versions_match:
                logger.warning(
                    "retrospective_terminal_override_stale "
                    "participant_id=%s previous_forecast_version=%s "
                    "retrospective_source_forecast_version=%s local_date=%s",
                    participant_id,
                    previous_version,
                    retrospective_version,
                    previous_date.isoformat(),
                    extra={
                        "participant_id": str(participant_id),
                        "previous_forecast_version": previous_version,
                        "retrospective_source_forecast_version": retrospective_version,
                        "local_date": previous_date.isoformat(),
                    },
                )
            elif retrospective is not None:
                forward_state = dict(
                    (retrospective.get("analysis") or {}).get(
                        "forward_terminal_state"
                    ) or {}
                )
                if forward_state:
                    terminal_override = {
                        **forward_state,
                        "retrospective_id": retrospective["id"],
                        "daily_review_revision": retrospective[
                            "daily_review_revision"
                        ],
                    }
        return self.initial_states.resolve(
            target,
            local_today,
            previous_day_forecast=previous,
            previous_day_terminal_override=terminal_override,
            baseline_state=self._profile_initial_state(effective_profile),
        )

    @staticmethod
    def _profile_initial_state(profile: dict[str, Any]) -> dict[str, float]:
        parameters = dict(profile.get("model_params") or profile.get("params") or {})
        ctssm = dict(parameters.get("ctssm_params") or {})
        try:
            stress = float(parameters.get("S_star_init", 40.0)) / 10.0
        except (TypeError, ValueError):
            stress = 4.0
        try:
            vitality = float(ctssm.get("vitality_baseline", 70.0)) / 10.0
        except (TypeError, ValueError):
            vitality = 7.0
        return {
            "stress_0_10": max(0.0, min(stress, 10.0)),
            "vitality_0_10": max(0.0, min(vitality, 10.0)),
        }

    def _warning_windows(self, alerts: Any, target: date) -> list[dict[str, Any]]:
        result = []
        for index, alert in enumerate(alerts):
            if not isinstance(alert, dict):
                continue
            try:
                hour, minute = (int(part) for part in str(alert.get("time") or "00:00")[:5].split(":"))
                risk_time = datetime.combine(target, time(hour, minute), self.timezone)
            except (TypeError, ValueError):
                continue
            target_time = risk_time - timedelta(minutes=self.warning_lead_minutes)
            valid_until = min(
                target_time + timedelta(minutes=self.warning_late_grace_minutes),
                risk_time,
            )
            level = str(alert.get("tier") or alert.get("intensity_zone") or "1")
            stressors = sorted(str(value) for value in (alert.get("dominant_stressors") or []))
            current_events = sorted(str(value) for value in (alert.get("current_events") or []))
            trigger = stressors or current_events or [
                str(alert.get("trigger_source") or "trajectory_episode")
            ]
            trigger_fingerprint = WarningScheduleRepository.episode_trigger_fingerprint(
                alert, default_source="trajectory_episode",
            )
            # A model-side ordinal is unstable when an earlier episode
            # disappears on recomputation.  Trigger + a coarse risk anchor is
            # stable across ordinary drift while keeping opportunities four
            # hours apart distinct under the delivery policy.
            anchor_bucket = int((hour * 60 + minute + 60) // 120)
            episode_identity = _sha({
                "date": target.isoformat(), "trigger": trigger,
                "risk_anchor_2h": anchor_bucket,
            })
            identity = episode_identity
            result.append({
                "warning_identity": identity, "target_time": target_time.astimezone(timezone.utc),
                "episode_identity": episode_identity,
                "risk_time": risk_time.astimezone(timezone.utc),
                "valid_until": valid_until.astimezone(timezone.utc),
                "episode_drift_minutes": self.warning_episode_drift_minutes,
                "warning_level": level, "payload": {
                    **alert,
                    "risk_time": risk_time.isoformat(),
                    "episode_trigger_fingerprint": trigger_fingerprint,
                    "episode_trigger_fingerprint_version": 2,
                },
            })
        return result

    @staticmethod
    def _serializable_warning(item: dict[str, Any]) -> dict[str, Any]:
        return {
            **item,
            "target_time": item["target_time"].isoformat(),
            "risk_time": item["risk_time"].isoformat(),
            "valid_until": item["valid_until"].isoformat(),
        }

    @staticmethod
    def _semantic_delta(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> float:
        left = {
            str(item.get("id")): semantic_model_inputs(
                (item.get("metadata") or {}).get("semantic")
            ) for item in before
        }
        right = {
            str(item.get("id")): semantic_model_inputs(
                (item.get("metadata") or {}).get("semantic")
            ) for item in after
        }
        delta = 0.0
        for event_id in set(left) | set(right):
            for dimension in set(left.get(event_id, {})) | set(right.get(event_id, {})):
                delta = max(delta, abs(float(left.get(event_id, {}).get(dimension, 0.0)) - float(right.get(event_id, {}).get(dimension, 0.0))))
        return delta

    @classmethod
    def _semantic_input_delta(
        cls, before: list[dict[str, Any]], after_events: list[dict[str, Any]]
    ) -> float:
        before_events = [
            {"id": item.get("event_id"), "metadata": {"semantic": item.get("semantic")}}
            for item in before
        ]
        return cls._semantic_delta(before_events, after_events)
