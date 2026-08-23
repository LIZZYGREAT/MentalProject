"""Daily Review validation and deterministic retrospective reconstruction."""

from __future__ import annotations

from datetime import date, datetime, timezone
import hashlib
import json
from typing import Any
import uuid
from zoneinfo import ZoneInfo

from app.config import Settings
from app.repositories import ForecastSnapshotRepository, ObservationRepository
from app.repositories_daily_review import (
    DailyReviewResponseRepository,
    DailyReviewScheduleRepository,
    RetrospectiveCurveRepository,
)
from app.services.observation_smoother import FixedLagObservationSmoother
from app.services.retrospective_reconstructor import PEAK_PERIODS, RetrospectiveReconstructor


def _score(value: Any, field: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a number") from exc
    if not 0 <= result <= 10:
        raise ValueError(f"{field} must be between 0 and 10")
    return result


def _text(value: Any, field: str, maximum: int) -> str | None:
    result = str(value or "").strip()
    if len(result) > maximum:
        raise ValueError(f"{field} must be at most {maximum} characters")
    return result or None


class DailyReviewService:
    CARD_VERSION = DailyReviewScheduleRepository.CARD_VERSION

    def __init__(
        self,
        responses: DailyReviewResponseRepository,
        schedules: DailyReviewScheduleRepository,
        retrospectives: RetrospectiveCurveRepository,
        forecasts: ForecastSnapshotRepository,
        observations: ObservationRepository,
        settings: Settings,
    ):
        self.responses = responses
        self.schedules = schedules
        self.retrospectives = retrospectives
        self.forecasts = forecasts
        self.observations = observations
        self.settings = settings
        self.timezone = ZoneInfo(settings.daily_review_timezone)
        self.smoother = FixedLagObservationSmoother(
            settings.observation_smoothing_window_minutes
        )
        self.reconstructor = RetrospectiveReconstructor(
            morning_sigma=settings.retrospective_morning_sigma_minutes,
            end_sigma=settings.retrospective_end_sigma_minutes,
            peak_rise=settings.retrospective_peak_rise_minutes,
            peak_decay=settings.retrospective_peak_decay_minutes,
            max_delta_per_5_min=settings.retrospective_max_delta_per_5_min,
            end_state_gain=settings.daily_review_end_state_gain,
        )

    def submit(
        self, participant_id: uuid.UUID, *, callback_event_id: str,
        action: dict[str, Any], values: dict[str, Any],
        submitted_at: datetime | None = None,
    ) -> dict[str, Any]:
        if str(action.get("version") or "") != "1":
            raise ValueError("unsupported daily review card version")
        try:
            local_date = date.fromisoformat(str(action.get("local_date") or ""))
            schedule_id = uuid.UUID(str(action.get("schedule_id") or ""))
        except ValueError as exc:
            raise ValueError("invalid daily review identity") from exc
        schedule = self.schedules.get(schedule_id)
        if schedule is None or schedule["participant_id"] != str(participant_id):
            raise ValueError("daily review schedule does not belong to participant")
        if schedule["local_date"] != local_date.isoformat():
            raise ValueError("daily review date does not match schedule")
        if schedule["card_version"] != self.CARD_VERSION:
            raise ValueError("daily review card is no longer supported")
        now = submitted_at or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        today = now.astimezone(self.timezone).date()
        if local_date > today or (today - local_date).days > 1:
            raise ValueError("daily review date is outside the accepted window")
        peak_period = str(values.get("peak_period") or "")
        if peak_period not in PEAK_PERIODS:
            raise ValueError("peak_period is invalid")
        normalized = {
            "start_stress": _score(values.get("start_stress"), "start_stress"),
            "start_energy": _score(values.get("start_energy"), "start_energy"),
            "peak_stress": _score(values.get("peak_stress"), "peak_stress"),
            "peak_period": peak_period,
            "end_stress": _score(values.get("end_stress"), "end_stress"),
            "end_energy": _score(values.get("end_energy"), "end_energy"),
            "energy_consumption": _score(
                values.get("energy_consumption"), "energy_consumption"
            ),
            "main_stressor": _text(values.get("main_stressor"), "main_stressor", 300),
            "recovery_note": _text(values.get("recovery_note"), "recovery_note", 300),
            "free_text": _text(values.get("free_text"), "free_text", 1000),
        }
        response, created = self.responses.add(
            participant_id, local_date,
            callback_event_id=callback_event_id,
            submitted_at=now,
            card_version=self.CARD_VERSION,
            schedule_id=schedule_id,
            values=normalized,
            raw=dict(values),
        )
        retrospective = self.rebuild(participant_id, local_date, response=response)
        return {
            "response": response,
            "created": created,
            "retrospective": retrospective,
        }

    def rebuild(
        self, participant_id: uuid.UUID, local_date: date,
        *, response: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response = response or self.responses.latest(participant_id, local_date)
        if response is None:
            raise ValueError("daily review response not found")
        forecast = self.forecasts.latest(participant_id, local_date)
        if forecast is None:
            raise ValueError("source forecast not found")
        observations = self.observations.for_local_date(
            participant_id, local_date,
            timezone_name=self.settings.daily_review_timezone,
            limit=500,
        )
        observation_revision = self._revision([
            {"id": row["id"], "observed_at": row["observed_at"], "payload": row["payload"]}
            for row in observations
        ])
        smoothed, smoothing_diagnostics = self.smoother.smooth(
            forecast["curve"], observations,
            timezone_name=self.settings.daily_review_timezone,
        )
        curve, analysis, diagnostics = self.reconstructor.reconstruct(
            smoothed, response, timezone_name=self.settings.daily_review_timezone
        )
        reconstruction_version = self._revision({
            "forecast_version": forecast["forecast_version"],
            "response_id": response["id"],
            "response_revision": response["revision"],
            "observation_revision": observation_revision,
            "smoother": smoothing_diagnostics["version"],
            "algorithm": self.reconstructor.ALGORITHM_VERSION,
        })
        diagnostics["observation_smoothing"] = smoothing_diagnostics
        diagnostics["original_forecast_immutable"] = True
        return self.retrospectives.save(
            participant_id, local_date,
            source_forecast_id=uuid.UUID(forecast["id"]),
            source_forecast_version=forecast["forecast_version"],
            daily_review_response_id=uuid.UUID(response["id"]),
            daily_review_revision=response["revision"],
            observation_revision=observation_revision,
            algorithm_version=self.reconstructor.ALGORITHM_VERSION,
            reconstruction_version=reconstruction_version,
            curve_json=curve,
            analysis_json=analysis,
            diagnostics_json=diagnostics,
        )

    @staticmethod
    def _revision(value: Any) -> str:
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()
