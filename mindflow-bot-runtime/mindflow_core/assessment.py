"""Production adapter for the existing deterministic stress/vitality simulator.

The mathematical implementation remains in the repository's reviewed Python
algorithm/core_engine packages. This module converts ordinary structures into
that entry point and returns an ordinary immutable result.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
import sys
from typing import Any
from zoneinfo import ZoneInfo

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
from settings.model_defaults import DEFAULT_EVENT_END, DEFAULT_EVENT_START
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
    MODEL_VERSION = "mindflow-ctssm-runtime-v2"

    def __init__(self, timezone_name: str):
        self.timezone = ZoneInfo(timezone_name)

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
        target_date = local_date or datetime.now(self.timezone).date().isoformat()
        parameters = profile.get("model_params") or profile.get("params") or {}
        user = User(user_id="runtime", params=dict(parameters), load_from_file=False)
        prepared = prepare_event_instances(calendar_events, target_date)
        for item in prepared:
            start = normalize_event_datetime(
                item.get("start_time") or DEFAULT_EVENT_START,
                target_date,
                self.timezone,
            )
            end = normalize_event_datetime(
                item.get("end_time") or DEFAULT_EVENT_END,
                target_date,
                self.timezone,
            )
            # A legacy clock-only interval whose end is earlier than its start
            # has always represented an event crossing midnight. ISO values
            # already carry an unambiguous date, so this adjustment is only
            # needed when both original values omit one.
            if (
                end < start
                and not _has_explicit_date(item.get("start_time"))
                and not _has_explicit_date(item.get("end_time"))
            ):
                end += timedelta(days=1)
            item["start_time"] = start.strftime("%Y-%m-%d %H:%M:%S")
            item["end_time"] = end.strftime("%Y-%m-%d %H:%M:%S")
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


def _has_explicit_date(value: Any) -> bool:
    if isinstance(value, datetime):
        return True
    text = str(value or "").strip()
    return len(text) >= 10 and text[4:5] == "-" and text[7:8] == "-"


def normalize_event_datetime(
    value: Any,
    local_date: str,
    timezone: ZoneInfo,
) -> datetime:
    """Convert one calendar value to a naive model-local datetime.

    Offset-bearing ISO values are converted to the configured application
    timezone. Legacy clock and naive datetime values are already local wall
    time, so they are preserved without consulting the host timezone.
    """

    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            raise ValueError("calendar event time is empty")
        if len(text) >= 5 and text[2:3] == ":" and not _has_explicit_date(text):
            try:
                parsed_time = datetime.strptime(text, "%H:%M").time()
            except ValueError:
                parsed_time = datetime.strptime(text, "%H:%M:%S").time()
            parsed = datetime.combine(date.fromisoformat(local_date), parsed_time)
        else:
            iso_text = f"{text[:-1]}+00:00" if text.endswith(("Z", "z")) else text
            parsed = datetime.fromisoformat(iso_text)

    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone).replace(tzinfo=None)
    return parsed
