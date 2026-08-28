"""Date-scoped initial-state provenance for forecast calculation."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, timedelta
import hashlib
import json
import logging
from typing import Any


POINT_AT_T_MODEL_VERSION = 7
logger = logging.getLogger(__name__)


def _uses_point_at_t_curve(forecast: dict[str, Any]) -> bool:
    """Return whether ``curve[-1]`` is the 23:55 point, not the 24:00 terminal."""

    version = str(forecast.get("algorithm_version") or "")
    prefix = "mindflow-ctssm-runtime-v"
    if not version.startswith(prefix):
        return False
    try:
        return int(version[len(prefix):]) >= POINT_AT_T_MODEL_VERSION
    except ValueError:
        # A future/non-numeric version under the canonical runtime namespace
        # must fail closed instead of silently treating 23:55 as midnight.
        return True


def forecast_terminal_state(
    forecast: dict[str, Any] | None,
) -> tuple[float, float] | None:
    """Resolve a forecast's 24:00 terminal without misusing a 23:55 point.

    Legacy snapshots predate the point-at-t contract and may use the final
    curve row as their terminal approximation.  Current runtime snapshots must
    carry the explicit output contract.
    """

    if forecast is None:
        return None
    output = dict(forecast.get("output") or {})
    stress = output.get("stress_0_10")
    vitality = output.get("vitality_0_10")
    if (stress is None or vitality is None) and not _uses_point_at_t_curve(forecast):
        curve = list(forecast.get("curve") or [])
        terminal = dict(curve[-1]) if curve else {}
        stress = terminal.get("stress_0_10")
        vitality = terminal.get("vitality_0_10")
    try:
        return (
            max(0.0, min(float(stress), 10.0)),
            max(0.0, min(float(vitality), 10.0)),
        )
    except (TypeError, ValueError):
        if _uses_point_at_t_curve(forecast):
            logger.error(
                "forecast_terminal_contract_incomplete forecast_id=%s "
                "forecast_version=%s algorithm_version=%s",
                forecast.get("id"),
                forecast.get("forecast_version"),
                forecast.get("algorithm_version"),
            )
        return None


def _revision(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class ForecastInitialState:
    mode: str
    stress_0_10: float | None = None
    vitality_0_10: float | None = None
    source_local_date: str | None = None
    source_forecast_id: str | None = None
    source_forecast_version: str | None = None
    source_retrospective_id: str | None = None
    source_daily_review_revision: int | None = None
    revision: str = ""

    @property
    def model_override(self) -> dict[str, float] | None:
        if self.stress_0_10 is None or self.vitality_0_10 is None:
            return None
        return {
            "stress_0_10": self.stress_0_10,
            "vitality_0_10": self.vitality_0_10,
        }

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ForecastInitialStateResolver:
    """Resolve today/default/tomorrow semantics without recursive day rolling."""

    @staticmethod
    def _bounded(value: Any, fallback: float) -> float:
        try:
            return max(0.0, min(float(value), 10.0))
        except (TypeError, ValueError):
            return fallback

    def _baseline(
        self, baseline_state: dict[str, Any] | None
    ) -> tuple[float, float]:
        value = dict(baseline_state or {})
        return (
            self._bounded(value.get("stress_0_10"), 4.0),
            self._bounded(value.get("vitality_0_10"), 7.0),
        )

    def _previous_terminal(
        self, previous_day_forecast: dict[str, Any] | None
    ) -> tuple[float, float] | None:
        return forecast_terminal_state(previous_day_forecast)

    def resolve(
        self,
        target: date,
        local_today: date,
        *,
        previous_day_forecast: dict[str, Any] | None = None,
        previous_day_terminal_override: dict[str, Any] | None = None,
        baseline_state: dict[str, Any] | None = None,
    ) -> ForecastInitialState:
        baseline_stress, baseline_vitality = self._baseline(baseline_state)
        if target > local_today + timedelta(days=1):
            payload = {
                "mode": "future_trend_default",
                "stress_0_10": baseline_stress,
                "vitality_0_10": baseline_vitality,
            }
            return ForecastInitialState(**payload, revision=_revision(payload))

        terminal = None
        if previous_day_terminal_override:
            try:
                terminal = (
                    self._bounded(previous_day_terminal_override.get("stress_0_10"), baseline_stress),
                    self._bounded(previous_day_terminal_override.get("vitality_0_10"), baseline_vitality),
                )
            except (TypeError, ValueError):
                terminal = None
        terminal = terminal or self._previous_terminal(previous_day_forecast)
        if terminal is None:
            payload = {
                "mode": "profile_default",
                "stress_0_10": baseline_stress,
                "vitality_0_10": baseline_vitality,
            }
            return ForecastInitialState(**payload, revision=_revision(payload))

        stress, vitality = terminal
        has_review = bool(previous_day_terminal_override)
        payload = {
            "mode": "previous_day_daily_review" if has_review else "previous_day_forecast",
            "stress_0_10": stress,
            "vitality_0_10": vitality,
            "source_local_date": (target - timedelta(days=1)).isoformat(),
            "source_forecast_id": str(previous_day_forecast.get("id") or ""),
            "source_forecast_version": str(
                previous_day_forecast.get("forecast_version") or ""
            ),
            "source_retrospective_id": (
                str(previous_day_terminal_override.get("retrospective_id") or "")
                if has_review else None
            ),
            "source_daily_review_revision": (
                int(previous_day_terminal_override.get("daily_review_revision") or 0)
                if has_review else None
            ),
        }
        return ForecastInitialState(**payload, revision=_revision(payload))
