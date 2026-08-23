"""Date-scoped initial-state provenance for forecast calculation."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, timedelta
import hashlib
import json
from typing import Any


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

    def resolve(
        self,
        target: date,
        local_today: date,
        *,
        previous_day_forecast: dict[str, Any] | None = None,
    ) -> ForecastInitialState:
        if target == local_today:
            payload = {"mode": "observation_default"}
            return ForecastInitialState(
                mode="observation_default", revision=_revision(payload)
            )
        if target != local_today + timedelta(days=1):
            payload = {"mode": "default"}
            return ForecastInitialState(mode="default", revision=_revision(payload))
        if previous_day_forecast is None:
            raise ValueError("tomorrow forecast requires today's forecast")

        output = dict(previous_day_forecast.get("output") or {})
        stress = max(0.0, min(float(output["stress_0_10"]), 10.0))
        vitality = max(0.0, min(float(output["vitality_0_10"]), 10.0))
        payload = {
            "mode": "previous_day_forecast",
            "stress_0_10": stress,
            "vitality_0_10": vitality,
            "source_local_date": local_today.isoformat(),
            "source_forecast_id": str(previous_day_forecast.get("id") or ""),
            "source_forecast_version": str(
                previous_day_forecast.get("forecast_version") or ""
            ),
        }
        return ForecastInitialState(**payload, revision=_revision(payload))
