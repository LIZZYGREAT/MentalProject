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
)
from app.services.event_semantic_preprocessor import EventSemanticPreprocessor
from app.services.prediction_service import PredictionService
from app.services.warning_policy import WarningPolicy
from app.services.profile_calibration import layered_profile
from services.event_lifecycle import prepare_event_instances
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
        warning_max_daily_sends: int = 2,
        warning_min_interval_minutes: int = 240,
        learned_profiles: LearnedProfileRepository | None = None,
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
        self.warning_policy = WarningPolicy(
            max_daily_sends=warning_max_daily_sends,
            min_interval_minutes=warning_min_interval_minutes,
        )
        self.learned_profiles = learned_profiles
        self._inflight: dict[tuple[uuid.UUID, date], dict[str, Any]] = {}
        self._guard = asyncio.Lock()

    def _warning_revision(self) -> tuple[str, dict[str, object]]:
        payload = {
            **self.warning_policy.identity_payload(),
            "lead_minutes": self.warning_lead_minutes,
            "late_grace_minutes": self.warning_late_grace_minutes,
            "episode_drift_minutes": self.warning_episode_drift_minutes,
        }
        return _sha(payload), payload

    def _derive_warning_state(
        self, output: dict[str, Any], target: date
    ) -> tuple[
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[dict[str, Any]],
    ]:
        selected = self.warning_policy.select_daily_candidates(
            output.get("alerts") or []
        )
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
    ) -> dict[str, Any]:
        output = dict(snapshot.get("output") or {})
        selected, windows, serialized_windows = self._derive_warning_state(
            output, target
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
    ) -> dict[str, Any]:
        target = date.fromisoformat(local_date) if isinstance(local_date, str) else local_date
        key = (participant_id, target)
        async with self._guard:
            flight = self._inflight.get(key)
            if flight is None:
                flight = {
                    "refresh_calendar": refresh_calendar,
                    "followup_refresh": False,
                    "followup_reason": reason,
                    "followup_enrichment": enqueue_enrichment,
                }
                task = asyncio.create_task(
                    self._run_flight(
                        key, flight, participant_id, target, reason,
                        refresh_calendar=refresh_calendar,
                        enqueue_enrichment=enqueue_enrichment,
                    ),
                    name=f"forecast-{participant_id}-{target.isoformat()}",
                )
                flight["task"] = task
                self._inflight[key] = flight
            elif refresh_calendar and not flight["refresh_calendar"]:
                flight["followup_refresh"] = True
                flight["followup_reason"] = reason
                flight["followup_enrichment"] = (
                    flight["followup_enrichment"] or enqueue_enrichment
                )
            task = flight["task"]
        return await asyncio.shield(task)

    async def _run_flight(
        self, key: tuple[uuid.UUID, date], flight: dict[str, Any],
        participant_id: uuid.UUID, target: date, reason: str, *,
        refresh_calendar: bool, enqueue_enrichment: bool,
    ) -> dict[str, Any]:
        try:
            result = await self._ensure_once(
                participant_id, target, reason,
                refresh_calendar=refresh_calendar,
                enqueue_enrichment=enqueue_enrichment,
            )
            async with self._guard:
                followup_refresh = bool(
                    flight["followup_refresh"] and not flight["refresh_calendar"]
                )
                followup_reason = flight["followup_reason"]
                followup_enrichment = flight["followup_enrichment"]
                if followup_refresh:
                    flight["refresh_calendar"] = True
                    flight["followup_refresh"] = False
                elif self._inflight.get(key) is flight:
                    self._inflight.pop(key, None)
            if followup_refresh:
                result = await self._ensure_once(
                    participant_id, target, followup_reason,
                    refresh_calendar=True,
                    enqueue_enrichment=followup_enrichment,
                )
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
        calendar_snapshot, calendar_changed = await self._calendar_snapshot(
            participant_id, target, refresh_calendar
        )
        participant = await asyncio.to_thread(self.participants.get, participant_id)
        consent = bool(participant and participant.external_llm_consent_at)
        events = prepare_event_instances(calendar_snapshot["events"], target.isoformat())
        semantic_events, semantic_revision, semantic_status, misses = await asyncio.to_thread(
            self.semantics.prepare, participant_id, events, consent=consent
        )
        observations = await asyncio.to_thread(
            self.observations.recent, participant_id, 50
        )
        observation_revision = _sha(observations)
        profile_row = await asyncio.to_thread(self.profiles.current, participant_id)
        learned_row = (
            await asyncio.to_thread(self.learned_profiles.current, participant_id)
            if self.learned_profiles is not None else None
        )
        effective_profile, profile_layers = layered_profile(profile_row, learned_row)
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
            "observation_revision": observation_revision,
            "profile_revision": profile_revision,
            "algorithm_version": algorithm_version,
            "warning_revision": warning_revision,
        })
        if latest and latest["forecast_version"] == expected_version:
            cached = await self._reconcile_cached_warning_state(
                latest,
                target,
                warning_revision=warning_revision,
                warning_policy_config=warning_policy_config,
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
            )
            output["profile_layers"] = profile_layers
            output["profile_revision"] = profile_revision
            output["warning_revision"] = warning_revision
            output["warning_policy_config"] = warning_policy_config
            curve = list(output.get("trajectory") or [])
            peaks = sorted(curve, key=lambda point: float(point.get("stress_0_10") or 0.0), reverse=True)[:5]
            selected_alerts, warning_windows, serialized_windows = (
                self._derive_warning_state(output, target)
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
            "calendar_events": list(calendar_snapshot.get("events") or []),
        })
        if enqueue_enrichment and misses and consent:
            async def recompute() -> None:
                await self.ensure_forecast(
                    participant_id, target, "semantic_enrichment_completion",
                    refresh_calendar=False, enqueue_enrichment=False,
                )
            await self.semantics.enqueue(
                participant_id, misses, recompute,
                completion_key=(participant_id, target),
            )
        return result

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
