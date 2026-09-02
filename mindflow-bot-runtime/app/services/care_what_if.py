"""Side-effect-free Calendar scenario simulation for Stage 6."""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime
from typing import Any
import uuid

from app.services.forecast_coordinator import enforce_promoted_model_selection
from app.services.profile_calibration import layered_profile


WHAT_IF_VERSION = "care-what-if.v1"


class CareWhatIfSimulationService:
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
        affected_dates = [local_date]
        if destination_date != local_date:
            affected_dates.append(destination_date)
        affected_dates = sorted(affected_dates)
        current_by_date: dict[date, dict[str, Any]] = {}
        calendar_by_date: dict[date, dict[str, Any]] = {}
        for target in affected_dates:
            current = self.coordinator.forecasts.latest(participant_id, target)
            if current is None:
                raise LookupError(
                    f"current_forecast_not_found:{target.isoformat()}"
                )
            calendar = self.coordinator.calendar_snapshots.get(participant_id, target)
            if calendar is None:
                raise LookupError(
                    f"calendar_snapshot_not_found:{target.isoformat()}"
                )
            current_by_date[target] = current
            calendar_by_date[target] = calendar
        source_events = deepcopy(list(calendar_by_date[local_date].get("events") or []))
        changed = False
        semantic_by_id = {
            str(item.get("event_id")): item.get("semantic")
            for item in list(current_by_date[local_date].get("semantic_input") or [])
        }
        moved_event: dict[str, Any] | None = None
        for event in source_events:
            normalized_id = str(event.get("id") or event.get("event_id") or "")
            semantic = semantic_by_id.get(normalized_id)
            if semantic:
                event["metadata"] = {
                    **dict(event.get("metadata") or {}),
                    "semantic": semantic,
                }
            if normalized_id == str(event_id):
                event["start_time"] = start.isoformat()
                event["end_time"] = end.isoformat()
                moved_event = deepcopy(event)
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
            if target == local_date:
                events = source_events
            else:
                events = deepcopy(list(calendar.get("events") or []))
                events = [
                    event for event in events
                    if str(event.get("id") or event.get("event_id") or "")
                    != str(event_id)
                ]
                events.append(deepcopy(moved_event))
            generated_at = current.get("generated_at") or datetime.now(
                self.coordinator.timezone
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
            if (
                scenario.get("stress_0_10") is not None
                and scenario.get("vitality_0_10") is not None
            ):
                previous_scenario_terminal = {
                    "stress_0_10": float(scenario["stress_0_10"]),
                    "vitality_0_10": float(scenario["vitality_0_10"]),
                }
            else:
                previous_scenario_terminal = None
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
    def _metrics(curve: list[dict[str, Any]]) -> dict[str, float]:
        stress = [float(point.get("stress_0_10") or 0.0) for point in curve]
        workload = [float(point.get("workload") or 0.0) for point in curve]
        recoveries = [
            point for point in curve
            if float(point.get("recovery_resource") or 0.0) >= 0.35
        ]
        return {
            "peak_stress": round(max(stress) if stress else 0.0, 4),
            "high_stress_duration_minutes": float(sum(5 for value in stress if value >= 7.0)),
            "mean_workload": round(sum(workload) / len(workload), 4) if workload else 0.0,
            "recovery_windows": float(len(recoveries)),
        }
