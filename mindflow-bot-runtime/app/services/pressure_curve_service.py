"""Shared forecast, analysis, and rendering view for Feishu and Admin."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
import uuid
from zoneinfo import ZoneInfo

from app.services.curve_analysis import CurveAnalysis, analyze_curve
from app.services.forecast_coordinator import ForecastCoordinator
from app.services.pressure_curve_renderer import PressureCurveRenderer


@dataclass(frozen=True)
class PressureCurveView:
    forecast: dict[str, Any]
    analysis: CurveAnalysis
    png_bytes: bytes


class HistoricalForecastNotFoundError(LookupError):
    """A past date is read-only and has no persisted original forecast."""


class PressureCurveService:
    def __init__(
        self,
        coordinator: ForecastCoordinator,
        *,
        timezone_name: str,
        renderer: PressureCurveRenderer | None = None,
    ):
        self.coordinator = coordinator
        self.timezone = ZoneInfo(timezone_name)
        self.renderer = renderer or PressureCurveRenderer()

    async def build(
        self,
        participant_id: uuid.UUID,
        local_date: date | str,
        *,
        reason: str,
        refresh_calendar: bool = True,
        stress_only: bool = False,
    ) -> PressureCurveView:
        target = date.fromisoformat(local_date) if isinstance(local_date, str) else local_date
        if target < datetime.now(self.timezone).date():
            forecast = await asyncio.to_thread(
                self.coordinator.forecasts.latest, participant_id, target
            )
            if forecast is None:
                raise HistoricalForecastNotFoundError(target.isoformat())
            calendar_snapshot = await asyncio.to_thread(
                self.coordinator.calendar_snapshots.get, participant_id, target
            )
            matching_calendar = bool(
                calendar_snapshot
                and calendar_snapshot.get("calendar_revision")
                == forecast.get("calendar_revision")
            )
            forecast = {
                **forecast,
                "calendar_events": list(
                    (calendar_snapshot or {}).get("events") or []
                ),
                "calendar_degraded": bool(
                    (forecast.get("output") or {}).get("calendar_degraded")
                ),
            }
            if not matching_calendar:
                forecast["calendar_events"] = []
        else:
            forecast = await self.coordinator.ensure_forecast(
                participant_id,
                target,
                reason,
                refresh_calendar=refresh_calendar,
            )
        curve = list(forecast.get("curve") or [])
        analysis = analyze_curve(
            curve,
            warning_windows=list(forecast.get("warning_windows") or []),
            calendar_events=list(forecast.get("calendar_events") or []),
            now=datetime.now(self.timezone),
            timezone_value=self.timezone,
        )
        png_bytes = await asyncio.to_thread(
            self.renderer.render,
            curve,
            analysis,
            dict(forecast.get("output") or {}),
            stress_only=stress_only,
        )
        return PressureCurveView(forecast, analysis, png_bytes)
