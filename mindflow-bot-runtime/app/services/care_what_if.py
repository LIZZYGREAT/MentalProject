"""Side-effect-free Calendar scenario simulation for Stage 6."""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta
from typing import Any
import uuid
from zoneinfo import ZoneInfo

from app.services.forecast_coordinator import enforce_promoted_model_selection
from app.services.profile_calibration import layered_profile


WHAT_IF_VERSION = "care-what-if.v1"


class CareWhatIfSimulationService:
    CLASSIFICATION_FIELDS = (
        "event_type",
        "task_type",
        "course_name",
        "course_code",
        "related_course_name",
        "related_course_code",
        "course_match_confidence",
        "course_match_source",
        "course_catalog_revision",
    )

    def __init__(self, coordinator: Any):
        self.coordinator = coordinator

    def simulate(
        self,
        participant_id: uuid.UUID,
        local_date: date,
        *,
        event_id: str,
        new_start_time: str,
        new_end_time: str,
    ) -> dict[str, Any]:
        start = datetime.fromisoformat(str(new_start_time).replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(new_end_time).replace("Z", "+00:00"))
        if start.tzinfo is None or end.tzinfo is None or end <= start:
            raise ValueError("what-if times must be timezone-aware and end after start")
        destination_date = start.astimezone(self.coordinator.timezone).date()
        first_date = min(local_date, destination_date)
        last_date = max(local_date, destination_date)
        affected_dates = [
            first_date + timedelta(days=offset)
            for offset in range((last_date - first_date).days + 1)
        ]
        current_by_date: dict[date, dict[str, Any]] = {}
        calendar_by_date: dict[date, dict[str, Any]] = {}
        forecast_events_by_date: dict[date, list[dict[str, Any]]] = {}
        for target in affected_dates:
            current = self.coordinator.forecasts.latest(participant_id, target)
            calendar = self.coordinator.calendar_snapshots.get(participant_id, target)
            if current is None or calendar is None:
                raise LookupError(f"scenario_input_unavailable:{target.isoformat()}")
            current_by_date[target] = current
            calendar_by_date[target] = calendar
            forecast_events_by_date[target] = self._rebuild_forecast_events(
                current, calendar
            )
        source_events = forecast_events_by_date[local_date]
        changed = False
        moved_event: dict[str, Any] | None = None
        for event in source_events:
            normalized_id = str(event.get("id") or event.get("event_id") or "")
            if normalized_id == str(event_id):
                moved_event = deepcopy(event)
                moved_event["start_time"] = start.isoformat()
                moved_event["end_time"] = end.isoformat()
                changed = True
        if not changed:
            raise LookupError("calendar_event_not_found")
        explicit = self.coordinator.profiles.current(participant_id)
        learned = (
            self.coordinator.learned_profiles.runtime_active(participant_id)
            if self.coordinator.learned_profiles is not None else None
        )
        profile, _layers = layered_profile(explicit, learned)
        profile = enforce_promoted_model_selection(profile, learned)
        original_curves: list[dict[str, Any]] = []
        scenario_curves: list[dict[str, Any]] = []
        per_date: dict[str, Any] = {}
        previous_scenario_terminal: dict[str, float] | None = None
        previous_target: date | None = None
        for target in affected_dates:
            current = current_by_date[target]
            calendar = calendar_by_date[target]
            original_curve = list(current.get("curve") or [])
            original_curves.extend(original_curve)
            events = deepcopy(forecast_events_by_date[target])
            if target == local_date:
                events = [
                    event for event in events
                    if str(event.get("id") or event.get("event_id") or "")
                    != str(event_id)
                ]
            if target == destination_date:
                events = [
                    event for event in events
                    if str(event.get("id") or event.get("event_id") or "")
                    != str(event_id)
                ]
                events.append(deepcopy(moved_event))
            generated_at = self._generated_at(
                current.get("generated_at"), self.coordinator.timezone
            )
            observations = self.coordinator.observations.for_local_date(
                participant_id,
                target,
                timezone_name=self.coordinator.timezone.key,
                as_of=generated_at,
                limit=100,
            )
            initial = dict((current.get("output") or {}).get("initial_state") or {})
            initial_override = None
            if initial.get("stress_0_10") is not None and initial.get("vitality_0_10") is not None:
                initial_override = {
                    "stress_0_10": initial["stress_0_10"],
                    "vitality_0_10": initial["vitality_0_10"],
                }
            if (
                previous_scenario_terminal is not None
                and previous_target is not None
                and (target - previous_target).days == 1
            ):
                initial_override = dict(previous_scenario_terminal)
            scenario = self.coordinator.prediction.calculate(
                profile=profile,
                observations=observations,
                calendar_events=events,
                calendar_degraded=bool(calendar.get("degraded")),
                local_date=target.isoformat(),
                initial_state=initial_override,
            )
            scenario_curve = list(scenario.get("trajectory") or [])
            previous_scenario_terminal = self._terminal_state(scenario)
            previous_target = target
            scenario_curves.extend(scenario_curve)
            per_date[target.isoformat()] = {
                "source_forecast_id": str(current["id"]),
                "source_forecast_version": current["forecast_version"],
                "original": self._metrics(original_curve),
                "scenario": self._metrics(scenario_curve),
            }
        original_metrics = self._metrics(original_curves)
        scenario_metrics = self._metrics(scenario_curves)
        source_current = current_by_date[local_date]
        return {
            "schema_version": WHAT_IF_VERSION,
            "simulation_only": True,
            "calendar_mutated": False,
            "source_forecast_id": str(source_current["id"]),
            "source_forecast_version": source_current["forecast_version"],
            "affected_dates": [target.isoformat() for target in affected_dates],
            "event_id": str(event_id),
            "proposed_change": {
                "start_time": start.isoformat(),
                "end_time": end.isoformat(),
            },
            "original": original_metrics,
            "scenario": scenario_metrics,
            "difference": {
                key: round(float(scenario_metrics[key]) - float(original_metrics[key]), 4)
                for key in original_metrics
            },
            "per_date": per_date,
            "confirmation_required_before_calendar_mutation": True,
        }

    @staticmethod
    def _generated_at(value: Any, timezone_value: ZoneInfo) -> datetime:
        if isinstance(value, datetime):
            parsed = value
        else:
            text = str(value or "").strip()
            if not text:
                return datetime.now(timezone_value)
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("source forecast generated_at must be timezone-aware")
        return parsed

    @classmethod
    def _rebuild_forecast_events(
        cls,
        current_forecast: dict[str, Any],
        calendar_snapshot: dict[str, Any],
    ) -> list[dict[str, Any]]:
        semantic_by_id = {
            str(item.get("event_id") or item.get("id") or ""): deepcopy(
                item.get("semantic")
            )
            for item in list(current_forecast.get("semantic_input") or [])
            if item.get("semantic") is not None
        }
        classified_by_id = {
            str(item.get("id") or item.get("event_id") or ""): item
            for item in list(
                (current_forecast.get("output") or {}).get(
                    "classified_calendar_events"
                )
                or []
            )
        }
        rebuilt: list[dict[str, Any]] = []
        for raw_event in list(calendar_snapshot.get("events") or []):
            event = deepcopy(raw_event)
            event_id = str(event.get("id") or event.get("event_id") or "")
            classified = classified_by_id.get(event_id)
            if classified is not None:
                for field in cls.CLASSIFICATION_FIELDS:
                    value = classified.get(field)
                    if value is not None:
                        event[field] = deepcopy(value)
            semantic = semantic_by_id.get(event_id)
            if semantic is not None:
                event["metadata"] = {
                    **dict(event.get("metadata") or {}),
                    "semantic": semantic,
                }
            rebuilt.append(event)
        return rebuilt

    @staticmethod
    def _terminal_state(result: dict[str, Any]) -> dict[str, float] | None:
        terminal = dict(result.get("terminal_state") or {})
        curve = list(result.get("trajectory") or [])
        if not terminal and curve:
            terminal = dict(curve[-1])
        stress = result.get("stress_0_10", terminal.get("stress_0_10"))
        vitality = result.get("vitality_0_10", terminal.get("vitality_0_10"))
        if stress is None or vitality is None:
            return None
        return {
            "stress_0_10": float(stress),
            "vitality_0_10": float(vitality),
        }

    @staticmethod
    def _metrics(curve: list[dict[str, Any]]) -> dict[str, float]:
        stress = [float(point.get("stress_0_10") or 0.0) for point in curve]
        workload = [float(point.get("workload") or 0.0) for point in curve]
        recovery_flags = [
            float(point.get("recovery_resource") or 0.0) >= 0.35
            for point in curve
        ]
        recovery_window_count = sum(
            1
            for index, active in enumerate(recovery_flags)
            if active and (index == 0 or not recovery_flags[index - 1])
        )
        recovery_duration = sum(5 for active in recovery_flags if active)
        return {
            "peak_stress": round(max(stress) if stress else 0.0, 4),
            "high_stress_duration_minutes": float(sum(5 for value in stress if value >= 7.0)),
            "mean_workload": round(sum(workload) / len(workload), 4) if workload else 0.0,
            "recovery_window_count": float(recovery_window_count),
            "recovery_duration_minutes": float(recovery_duration),
        }
