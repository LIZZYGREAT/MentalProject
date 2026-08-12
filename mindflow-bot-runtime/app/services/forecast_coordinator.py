"""One versioned entry point for calendar freshness, forecast, and warnings."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
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
    WarningScheduleRepository,
)
from app.services.event_semantic_preprocessor import EventSemanticPreprocessor
from app.services.prediction_service import PredictionService
from services.event_lifecycle import prepare_event_instances
from services.semantic_model_inputs import semantic_model_inputs


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
        self._locks: dict[tuple[uuid.UUID, date], asyncio.Lock] = {}
        self._dirty: set[tuple[uuid.UUID, date]] = set()
        self._lock_users: dict[tuple[uuid.UUID, date], int] = {}
        self._guard = asyncio.Lock()

    async def _lock_for(self, key: tuple[uuid.UUID, date]) -> asyncio.Lock:
        async with self._guard:
            self._lock_users[key] = self._lock_users.get(key, 0) + 1
            return self._locks.setdefault(key, asyncio.Lock())

    async def _release_lock(self, key: tuple[uuid.UUID, date]) -> None:
        async with self._guard:
            remaining = self._lock_users.get(key, 1) - 1
            if remaining <= 0 and key not in self._dirty:
                self._lock_users.pop(key, None)
                self._locks.pop(key, None)
            else:
                self._lock_users[key] = remaining

    async def ensure_forecast(
        self, participant_id: uuid.UUID, local_date: date | str, reason: str,
        *, refresh_calendar: bool = True, enqueue_enrichment: bool = True,
    ) -> dict[str, Any]:
        target = date.fromisoformat(local_date) if isinstance(local_date, str) else local_date
        key = (participant_id, target)
        lock = await self._lock_for(key)
        if lock.locked():
            self._dirty.add(key)
        try:
            async with lock:
                latest_result: dict[str, Any] | None = None
                while True:
                    self._dirty.discard(key)
                    latest_result = await self._ensure_once(
                        participant_id, target, reason,
                        refresh_calendar=refresh_calendar,
                        enqueue_enrichment=enqueue_enrichment,
                    )
                    if key not in self._dirty:
                        return latest_result
                    refresh_calendar = True
        finally:
            await self._release_lock(key)

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
        latest = await asyncio.to_thread(self.forecasts.latest, participant_id, target)
        algorithm_version = str(self.prediction.model.MODEL_VERSION)
        expected_version = _sha({
            "calendar_revision": calendar_snapshot["calendar_revision"],
            "semantic_revision": semantic_revision,
            "algorithm_version": algorithm_version,
        })
        if latest and latest["forecast_version"] == expected_version:
            result = {**latest, "cache_hit": True, "calendar_changed": calendar_changed, "reason": reason}
        elif (
            latest
            and latest["calendar_revision"] == calendar_snapshot["calendar_revision"]
            and latest["algorithm_version"] == algorithm_version
            and self._semantic_input_delta(latest["semantic_input"], semantic_events)
            < self.materiality_threshold
        ):
            # Persisted forecast remains the effective semantic version until
            # enrichment changes enough to affect the model.  This keeps later
            # curve requests from undoing the materiality decision.
            result = {
                **latest, "cache_hit": True, "calendar_changed": calendar_changed,
                "material_change": False, "reason": reason,
            }
        else:
            profile_row = await asyncio.to_thread(self.profiles.current, participant_id)
            observations = await asyncio.to_thread(self.observations.recent, participant_id, 50)
            output = await asyncio.to_thread(
                self.prediction.calculate,
                profile=profile_row["profile"] if profile_row else {},
                observations=observations, calendar_events=semantic_events,
                calendar_degraded=calendar_snapshot["degraded"], local_date=target.isoformat(),
            )
            curve = list(output.get("trajectory") or [])
            peaks = sorted(curve, key=lambda point: float(point.get("stress_0_10") or 0.0), reverse=True)[:5]
            warning_windows = self._warning_windows(output.get("alerts") or [], target)
            semantic_input = [
                {"event_id": item.get("id"), "semantic": (item.get("metadata") or {}).get("semantic")}
                for item in semantic_events
            ]
            saved = await asyncio.to_thread(
                self.forecasts.save, participant_id, target,
                calendar_revision=calendar_snapshot["calendar_revision"],
                semantic_revision=semantic_revision, algorithm_version=algorithm_version,
                forecast_version=expected_version, semantic_status=semantic_status,
                semantic_input=semantic_input, curve=curve, peaks=peaks,
                warning_windows=[self._serializable_warning(item) for item in warning_windows],
                output=output,
            )
            warning_diff = await asyncio.to_thread(
                self.warnings.sync, participant_id, target,
                forecast_id=uuid.UUID(saved["id"]), forecast_version=expected_version,
                warnings=warning_windows, now=datetime.now(timezone.utc),
            )
            result = {**saved, "cache_hit": False, "calendar_changed": calendar_changed,
                      "warning_diff": warning_diff, "reason": reason}
        result.update({
            "calendar_fresh": not calendar_snapshot["degraded"],
            "calendar_stale": bool(calendar_snapshot["degraded"]),
            "calendar_degraded": bool(calendar_snapshot["degraded"]),
            "calendar_last_refresh_success_at": calendar_snapshot.get("last_refresh_success_at"),
            "calendar_last_refresh_error_class": calendar_snapshot.get("last_refresh_error_class"),
        })
        if enqueue_enrichment and misses and consent:
            async def recompute() -> None:
                before = semantic_revision
                refreshed_events, after, _, _ = await asyncio.to_thread(
                    self.semantics.prepare, participant_id, events, consent=True
                )
                if self._semantic_delta(semantic_events, refreshed_events) >= self.materiality_threshold and after != before:
                    await self.ensure_forecast(
                        participant_id, target, "semantic_enrichment_completion",
                        refresh_calendar=False, enqueue_enrichment=False,
                    )
            await self.semantics.enqueue(participant_id, misses, recompute)
        return result

    def _warning_windows(self, alerts: Any, target: date) -> list[dict[str, Any]]:
        result = []
        trigger_counts: dict[str, int] = {}
        for index, alert in enumerate(alerts):
            if not isinstance(alert, dict):
                continue
            try:
                hour, minute = (int(part) for part in str(alert.get("time") or "00:00")[:5].split(":"))
                risk_time = datetime.combine(target, time(hour, minute), self.timezone)
            except (TypeError, ValueError):
                continue
            target_time = risk_time - timedelta(minutes=self.warning_lead_minutes)
            valid_until = risk_time + timedelta(minutes=self.warning_late_grace_minutes)
            level = str(alert.get("tier") or alert.get("intensity_zone") or "1")
            stressors = sorted(str(value) for value in (alert.get("dominant_stressors") or []))
            current_events = sorted(str(value) for value in (alert.get("current_events") or []))
            trigger = stressors or current_events or [
                str(alert.get("trigger_source") or "trajectory_episode")
            ]
            trigger_key = _canonical(trigger)
            ordinal = trigger_counts.get(trigger_key, 0)
            trigger_counts[trigger_key] = ordinal + 1
            episode_identity = _sha({
                "date": target.isoformat(), "trigger": trigger, "ordinal": ordinal,
            })
            identity = episode_identity
            result.append({
                "warning_identity": identity, "target_time": target_time.astimezone(timezone.utc),
                "episode_identity": episode_identity,
                "risk_time": risk_time.astimezone(timezone.utc),
                "valid_until": valid_until.astimezone(timezone.utc),
                "episode_drift_minutes": self.warning_episode_drift_minutes,
                "warning_level": level, "payload": {**alert, "risk_time": risk_time.isoformat()},
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
