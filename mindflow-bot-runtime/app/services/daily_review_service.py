"""Daily Review validation and deterministic retrospective reconstruction."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from typing import Any
import uuid
from zoneinfo import ZoneInfo

from app.config import Settings
from app.repositories import (
    ForecastSnapshotRepository,
    ObservationRepository,
    WarningScheduleRepository,
)
from app.repositories_daily_review import (
    DailyReviewResponseRepository,
    DailyReviewScheduleRepository,
    RetrospectiveCurveRepository,
)
from app.services.observation_smoother import FixedLagObservationSmoother
from app.services.retrospective_reconstructor import PEAK_PERIODS, RetrospectiveReconstructor
from app.services.forecast_dependency_refresh import ForecastDependencyRefreshService
from app.services.forecast_initial_state import forecast_terminal_state


def _score(value: Any, field: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a number") from exc
    if not 0 <= result <= 10:
        raise ValueError(f"{field} must be between 0 and 10")
    return result


def _optional_score(value: Any, field: str) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    return _score(value, field)


def _text(value: Any, field: str, maximum: int) -> str | None:
    result = str(value or "").strip()
    if len(result) > maximum:
        raise ValueError(f"{field} must be at most {maximum} characters")
    return result or None


def _stored_datetime(value: str | datetime) -> datetime:
    """Parse repository timestamps, treating SQLite's naive values as UTC."""

    parsed = (
        value
        if isinstance(value, datetime)
        else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    )
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class DailyReviewEndAnchor:
    minute: int
    source: str
    review_local_date: date
    submitted_local_date: date


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
        self.warning_repository: WarningScheduleRepository | None = None
        self.dependency_refresh: ForecastDependencyRefreshService | None = None
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

        existing_response = self.responses.get_by_callback_event_id(
            participant_id, callback_event_id
        )
        if existing_response is not None:
            return self._accepted_callback_result(
                participant_id,
                local_date,
                schedule_id,
                schedule,
                existing_response,
            )

        now = submitted_at or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        else:
            now = now.astimezone(timezone.utc)
        try:
            today = now.astimezone(self.timezone).date()
            if local_date > today or (today - local_date).days > 1:
                raise ValueError("daily review date is outside the accepted window")
            # Validate model-time and submission-window semantics before a new
            # append-only response is accepted.
            self._end_anchor(local_date, schedule, now)
            self._validate_submission_window(schedule, now)
        except ValueError:
            # A concurrent request may have accepted this callback just before
            # the validity boundary. Re-check before rejecting it as a new event.
            concurrent_response = self.responses.get_by_callback_event_id(
                participant_id, callback_event_id
            )
            if concurrent_response is not None:
                return self._accepted_callback_result(
                    participant_id,
                    local_date,
                    schedule_id,
                    schedule,
                    concurrent_response,
                )
            raise
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
            "energy_consumption": _optional_score(
                values.get("energy_consumption"), "energy_consumption"
            ),
            "main_stressor": _text(values.get("main_stressor"), "main_stressor", 300),
            "recovery_note": _text(values.get("recovery_note"), "recovery_note", 300),
            "free_text": _text(values.get("free_text"), "free_text", 1000),
        }
        source_forecast = self.forecasts.current_at(
            participant_id, local_date, now
        )
        response, created = self.responses.add(
            participant_id, local_date,
            callback_event_id=callback_event_id,
            submitted_at=now,
            card_version=self.CARD_VERSION,
            schedule_id=schedule_id,
            causal_source_forecast_id=(
                source_forecast["id"] if source_forecast is not None else None
            ),
            causal_source_forecast_version=(
                source_forecast["forecast_version"]
                if source_forecast is not None
                else None
            ),
            values=normalized,
            raw=dict(values),
        )
        if not created:
            return self._accepted_callback_result(
                participant_id,
                local_date,
                schedule_id,
                schedule,
                response,
            )
        end_anchor = self._end_anchor(
            local_date, schedule, _stored_datetime(response["submitted_at"])
        )
        retrospective = (
            self.rebuild(
                participant_id,
                local_date,
                response=response,
                end_anchor=end_anchor,
                use_response_causal_source=True,
            )
            if source_forecast is not None
            else None
        )
        return {
            "response": response,
            "created": created,
            "retrospective": retrospective,
        }

    def _accepted_callback_result(
        self,
        participant_id: uuid.UUID,
        local_date: date,
        schedule_id: uuid.UUID,
        schedule: dict[str, Any],
        response: dict[str, Any],
    ) -> dict[str, Any]:
        if (
            response["local_date"] != local_date.isoformat()
            or response.get("schedule_id") != str(schedule_id)
            or response["card_version"] != self.CARD_VERSION
        ):
            raise ValueError(
                "daily review callback identity does not match persisted response"
            )
        original_submitted_at = _stored_datetime(response["submitted_at"])
        # Validate the persisted audit fact, never the transport retry time.
        self._validate_submission_window(schedule, original_submitted_at)
        end_anchor = self._end_anchor(
            local_date, schedule, original_submitted_at
        )
        retrospective = self.retrospectives.latest_for_response(
            participant_id, response["id"]
        )
        if (
            retrospective is None
            and response.get("causal_source_forecast_id")
            and response.get("causal_source_forecast_version")
        ):
            retrospective = self.rebuild(
                participant_id,
                local_date,
                response=response,
                end_anchor=end_anchor,
                use_response_causal_source=True,
            )
        return {
            "response": response,
            "created": False,
            "retrospective": retrospective,
        }

    def rebuild(
        self, participant_id: uuid.UUID, local_date: date,
        *, response: dict[str, Any] | None = None,
        end_anchor: DailyReviewEndAnchor | None = None,
        use_response_causal_source: bool = True,
    ) -> dict[str, Any]:
        response = response or self.responses.latest(participant_id, local_date)
        if response is None:
            raise ValueError("daily review response not found")
        if end_anchor is None:
            schedule_id = response.get("schedule_id")
            schedule = self.schedules.get(schedule_id) if schedule_id else None
            if schedule is None or schedule["participant_id"] != str(participant_id):
                raise ValueError("daily review schedule not found for reconstruction")
            if schedule["local_date"] != local_date.isoformat():
                raise ValueError("daily review date does not match schedule")
            end_anchor = self._end_anchor(
                local_date, schedule, _stored_datetime(response["submitted_at"])
            )
        if use_response_causal_source:
            causal_cutoff = _stored_datetime(response["submitted_at"])
            forecast_id = response.get("causal_source_forecast_id")
            forecast_version = response.get("causal_source_forecast_version")
            if not forecast_id or not forecast_version:
                raise ValueError(
                    "daily review causal source forecast is unresolved"
                )
            forecast = self.forecasts.get(
                participant_id, forecast_id, local_date=local_date
            )
            if (
                forecast is None
                or forecast["forecast_version"] != forecast_version
            ):
                raise ValueError(
                    "daily review causal source forecast does not match response"
                )
        else:
            causal_cutoff = None
            forecast = self.forecasts.latest(participant_id, local_date)
        if forecast is None:
            raise ValueError("source forecast not found")
        observations = self.observations.for_local_date(
            participant_id, local_date,
            timezone_name=self.settings.daily_review_timezone,
            as_of=causal_cutoff,
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
            smoothed,
            response,
            source_terminal_state=(
                {
                    "stress_0_10": terminal[0],
                    "vitality_0_10": terminal[1],
                }
                if (terminal := forecast_terminal_state(forecast)) is not None
                else None
            ),
            end_anchor_minute=end_anchor.minute,
            end_anchor_source=end_anchor.source,
            review_local_date=end_anchor.review_local_date.isoformat(),
            submitted_local_date=end_anchor.submitted_local_date.isoformat(),
        )
        reconstruction_version = self._revision({
            "forecast_version": forecast["forecast_version"],
            "response_id": response["id"],
            "response_revision": response["revision"],
            "observation_revision": observation_revision,
            "smoother": smoothing_diagnostics["version"],
            "algorithm": self.reconstructor.ALGORITHM_VERSION,
            "end_anchor_minute": end_anchor.minute,
            "end_anchor_source": end_anchor.source,
            "review_local_date": end_anchor.review_local_date.isoformat(),
            "submitted_local_date": end_anchor.submitted_local_date.isoformat(),
        })
        diagnostics["observation_smoothing"] = smoothing_diagnostics
        diagnostics["original_forecast_immutable"] = True
        diagnostics["analysis_kind"] = (
            "causal" if use_response_causal_source else "reanalysis"
        )
        if not use_response_causal_source:
            return {
                "id": None,
                "participant_id": str(participant_id),
                "local_date": local_date.isoformat(),
                "source_forecast_id": forecast["id"],
                "source_forecast_version": forecast["forecast_version"],
                "daily_review_response_id": response["id"],
                "daily_review_revision": response["revision"],
                "observation_revision": observation_revision,
                "algorithm_version": self.reconstructor.ALGORITHM_VERSION,
                "reconstruction_version": reconstruction_version,
                "curve": curve,
                "analysis": analysis,
                "diagnostics": diagnostics,
                "analysis_kind": "reanalysis",
                "generated_at": datetime.now(timezone.utc).isoformat(),
            }
        saved, created = self.retrospectives.save(
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
        if created and self.dependency_refresh is not None:
            self.dependency_refresh.invalidate_dependent_now(
                participant_id,
                local_date,
                reason="previous_day_retrospective_terminal_changed",
            )
            self.dependency_refresh.enqueue_dependent_after_source(
                participant_id,
                local_date,
                reason="previous_day_retrospective_terminal_changed",
            )
        elif created and (
            self.warning_repository is not None
            and local_date
            in {
                datetime.now(self.timezone).date() - timedelta(days=1),
                datetime.now(self.timezone).date(),
            }
        ):
            self.forecasts.invalidate_current_for_date(
                self.warning_repository,
                participant_id,
                local_date + timedelta(days=1),
                reason="previous_day_retrospective_terminal_changed",
            )
        return saved

    def reanalysis(
        self, participant_id: uuid.UUID, local_date: date
    ) -> dict[str, Any]:
        """Build a latest-facts analysis without persisting causal state."""

        return self.rebuild(
            participant_id,
            local_date,
            use_response_causal_source=False,
        )

    def _end_anchor(
        self,
        local_date: date,
        schedule: dict[str, Any],
        submitted_at: datetime,
    ) -> DailyReviewEndAnchor:
        submitted_local = _stored_datetime(submitted_at).astimezone(self.timezone)
        scheduled_local = _stored_datetime(schedule["scheduled_at"]).astimezone(
            self.timezone
        )
        if scheduled_local.date() != local_date:
            raise ValueError("daily review scheduled time does not match local date")
        if submitted_local.date() < local_date:
            raise ValueError("daily review was submitted before its local date")
        if submitted_local.date() == local_date:
            anchor_local = submitted_local
            source = "same_day_submission"
        else:
            anchor_local = scheduled_local
            source = "scheduled_review_time"
        return DailyReviewEndAnchor(
            minute=anchor_local.hour * 60 + anchor_local.minute,
            source=source,
            review_local_date=local_date,
            submitted_local_date=submitted_local.date(),
        )

    @staticmethod
    def _validate_submission_window(
        schedule: dict[str, Any], submitted_at: datetime
    ) -> None:
        scheduled_at = _stored_datetime(schedule["scheduled_at"])
        valid_until = _stored_datetime(schedule["valid_until"])
        submitted = _stored_datetime(submitted_at)
        if valid_until < scheduled_at:
            raise ValueError("daily review schedule has an invalid validity window")
        if submitted < scheduled_at:
            raise ValueError("daily review was submitted before its scheduled time")
        if submitted > valid_until:
            raise ValueError("daily review submission window has expired")

    @staticmethod
    def _revision(value: Any) -> str:
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()
