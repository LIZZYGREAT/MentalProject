"""Production adapter for the existing deterministic stress/vitality simulator.

The mathematical implementation remains in the repository's reviewed Python
algorithm/core_engine packages. This module converts ordinary structures into
that entry point and returns an ordinary immutable result.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
import sys
from typing import Any

# Local source runs keep the reviewed model one directory above this standalone
# runtime. Container builds provide /srv/project through PYTHONPATH instead.
try:
    from entity.user import User
except ModuleNotFoundError:
    project_root = Path(__file__).resolve().parents[2]
    if (project_root / "entity").is_dir():
        sys.path.insert(0, str(project_root))
    from entity.user import User
from services.event_lifecycle import prepare_event_instances
from utils.event_factory import EventFactory


@dataclass(frozen=True)
class PredictionResult:
    model_version: str
    local_date: str
    stress_0_10: float
    vitality_0_10: float
    alert_count: int
    point_count: int
    calendar_event_count: int
    calendar_degraded: bool
    trajectory: tuple[dict[str, Any], ...]
    alerts: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AssessmentModel:
    MODEL_VERSION = "mindflow-ctssm-runtime-v1"

    @staticmethod
    def _latest_state(observations: list[dict[str, Any]]) -> tuple[float, float]:
        initial_stress = 40.0
        initial_vitality = 70.0
        if not observations:
            return initial_stress, initial_vitality
        payload = dict(observations[0].get("payload") or {})
        try:
            initial_stress = float(payload.get("stress_0_10", 4.0)) * 10.0
            initial_vitality = float(payload.get("energy_0_10", 7.0)) * 10.0
        except (TypeError, ValueError):
            pass
        return max(0.0, min(initial_stress, 100.0)), max(
            0.0, min(initial_vitality, 100.0)
        )

    def predict(
        self,
        *,
        profile: dict[str, Any],
        observations: list[dict[str, Any]],
        calendar_events: list[dict[str, Any]],
        local_date: str | None = None,
        calendar_degraded: bool = False,
    ) -> PredictionResult:
        target_date = local_date or date.today().isoformat()
        parameters = profile.get("model_params") or profile.get("params") or {}
        user = User(user_id="runtime", params=dict(parameters), load_from_file=False)
        prepared = prepare_event_instances(calendar_events, target_date)
        for item in prepared:
            metadata = dict(item.get("metadata") or {})
            metadata["allow_external_semantics"] = False
            item["metadata"] = metadata
        events = EventFactory.create_from_json(prepared)
        initial_stress, initial_vitality = self._latest_state(observations)
        model_observations = [
            {
                "target_time": item.get("observed_at"),
                "stress": (item.get("payload") or {}).get("stress_0_10"),
                "vitality": (item.get("payload") or {}).get("energy_0_10"),
            }
            for item in reversed(observations)
            if item.get("type") == "checkin"
        ]
        result = user.solver.simulate_day(
            events,
            initial_stress,
            initial_vitality,
            target_date,
            observations=model_observations,
        )
        points, end_stress, end_vitality, _, _, alerts, *_ = result
        trajectory = tuple(
            {
                "time": str(point.get("time") or ""),
                "stress_0_10": round(float(point.get("S") or 0.0) / 10.0, 3),
                "vitality_0_10": round(
                    float(point.get("V", point.get("E", 0.0)) or 0.0) / 10.0,
                    3,
                ),
                "state": str(point.get("state") or ""),
                "current_events": [
                    str(value)[:160] for value in (point.get("current_events") or [])[:10]
                ],
            }
            for point in (points or [])
        )
        safe_alerts = tuple(
            {
                str(key): value
                for key, value in alert.items()
                if isinstance(value, (str, int, float, bool, type(None)))
            }
            if isinstance(alert, dict)
            else {"message": str(alert)[:500]}
            for alert in (alerts or [])
        )
        return PredictionResult(
            model_version=self.MODEL_VERSION,
            local_date=target_date,
            stress_0_10=round(float(end_stress) / 10.0, 2),
            vitality_0_10=round(float(end_vitality) / 10.0, 2),
            alert_count=len(alerts or []),
            point_count=len(points or []),
            calendar_event_count=len(calendar_events),
            calendar_degraded=bool(calendar_degraded),
            trajectory=trajectory,
            alerts=safe_alerts,
        )
