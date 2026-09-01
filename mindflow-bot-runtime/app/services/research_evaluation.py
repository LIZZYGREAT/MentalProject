"""Stage-2 research matching, evaluation, quality, and snapshot services."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import math
from statistics import mean, median
from typing import Any
import uuid
from zoneinfo import ZoneInfo

from sqlalchemy import desc, select

from app.contracts.research import MOMENTARY_OBSERVATION_TYPES
from app.db import Database
from app.models import (
    CalendarSnapshot,
    CareInterventionEvent,
    CareInterventionFeedback,
    DailyReviewResponse,
    DatasetSnapshot,
    DatasetSnapshotItem,
    EventSemanticCache,
    EventAppraisalFeedback,
    ForecastCurrentnessEvent,
    ForecastObservationMatch,
    ForecastSnapshot,
    LearnedModelProfile,
    ModelEvaluationRun,
    Participant,
    ParticipantSlowState,
    PsychometricAssessment,
    StateObservation,
    WarningSchedule,
)
from app.repositories import ForecastSnapshotRepository, LearnedProfileRepository
from app.services.model_comparison import (
    MODEL_FAMILIES,
)
from app.services.stage4_candidate_replay import Stage4CandidateReplayService
from services.workload import WorkloadEstimator


DATASET_SCHEMA_V2 = "mindflow-research-dataset-v2"
DATASET_SCHEMA_V3 = "mindflow-research-dataset-v3"
DATASET_SCHEMA_V4 = "mindflow-research-dataset-v4"
DATASET_SCHEMA_V5 = "mindflow-research-dataset-v5"
DATASET_SCHEMA_VERSION = DATASET_SCHEMA_V5
STAGE5_INTERVENTION_EXCLUSION_MINUTES = 120
MATCH_SCHEMA_VERSION = "forecast-observation-grid.v2"
EVALUATION_CODE_VERSION = "stage4-evaluation.v7"
MATCH_TOLERANCE_SECONDS = 150
EVALUATION_MODES = {"historical_online", "offline_replay"}
MODEL_IDENTITY_FILTER_FIELDS = {
    "engine_version",
    "model_family",
    "model_variant",
    "model_spec_version",
    "promotion_decision_id",
    "promotion_parameters_hash",
}


def _aware(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, (date, datetime)) else None


def _score(payload: dict[str, Any], key: str) -> float | None:
    try:
        value = float(payload[key])
    except (KeyError, TypeError, ValueError):
        return None
    return value if 0 <= value <= 10 else None


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=lambda item: item.isoformat()
        if isinstance(item, (date, datetime, uuid.UUID))
        else str(item),
    )


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


class ResearchEvaluationService:
    def __init__(self, database: Database, timezone_name: str):
        self.database = database
        self.timezone = ZoneInfo(timezone_name)
        self.forecasts = ForecastSnapshotRepository(database)
        self.learned_profiles = LearnedProfileRepository(database)

    def _bounds(self, start: date, end: date) -> tuple[datetime, datetime]:
        if start > end or (end - start).days > 365:
            raise ValueError("invalid date range")
        lower = datetime.combine(start, time.min, self.timezone).astimezone(
            timezone.utc
        )
        upper = datetime.combine(
            end + timedelta(days=1), time.min, self.timezone
        ).astimezone(timezone.utc)
        return lower, upper

    @staticmethod
    def _correlation(pairs: list[tuple[float, float]]) -> float | None:
        if len(pairs) < 2:
            return None
        xs = [item[0] for item in pairs]
        ys = [item[1] for item in pairs]
        x_mean, y_mean = mean(xs), mean(ys)
        numerator = sum((x - x_mean) * (y - y_mean) for x, y in pairs)
        denominator = math.sqrt(
            sum((x - x_mean) ** 2 for x in xs)
            * sum((y - y_mean) ** 2 for y in ys)
        )
        return numerator / denominator if denominator > 1e-12 else None

    def workload_diagnostics(
        self,
        date_start: date,
        date_end: date,
        participant_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        """Compare persisted W(t) with Forecast stress and actual EMA stress."""

        lower, upper = self._bounds(date_start, date_end)
        forecast_conditions = [
            ForecastSnapshot.local_date >= date_start,
            ForecastSnapshot.local_date <= date_end,
            ForecastSnapshot.valid.is_(True),
        ]
        observation_conditions = [
            StateObservation.observed_at >= lower,
            StateObservation.observed_at < upper,
            StateObservation.observation_type.in_(MOMENTARY_OBSERVATION_TYPES),
        ]
        appraisal_conditions = [
            EventAppraisalFeedback.submitted_at >= lower,
            EventAppraisalFeedback.submitted_at < upper,
        ]
        if participant_id is not None:
            forecast_conditions.append(ForecastSnapshot.participant_id == participant_id)
            observation_conditions.append(StateObservation.participant_id == participant_id)
            appraisal_conditions.append(EventAppraisalFeedback.participant_id == participant_id)
        with self.database.session() as session:
            forecasts = session.execute(
                select(ForecastSnapshot)
                .where(*forecast_conditions)
                .order_by(ForecastSnapshot.local_date, ForecastSnapshot.participant_id)
            ).scalars().all()
            observations = session.execute(
                select(StateObservation)
                .where(*observation_conditions)
                .order_by(StateObservation.observed_at)
            ).scalars().all()
            appraisals = session.execute(
                select(EventAppraisalFeedback)
                .where(*appraisal_conditions)
                .order_by(EventAppraisalFeedback.submitted_at)
            ).scalars().all()

        forecast_series: list[dict[str, Any]] = []
        events: list[dict[str, Any]] = []
        recovery_windows: list[dict[str, Any]] = []
        forecast_pairs: list[tuple[float, float]] = []
        for row in forecasts:
            for point in list(row.curve_json or []):
                timestamp = self._point_time(row.local_date, point.get("time"), self.timezone)
                if timestamp is None:
                    continue
                try:
                    workload = float(point["workload"])
                    stress = float(point["stress_0_10"])
                except (KeyError, TypeError, ValueError):
                    continue
                if not (0.0 <= workload <= 1.0 and 0.0 <= stress <= 10.0):
                    continue
                forecast_pairs.append((workload, stress))
                if len(forecast_series) < 10000:
                    forecast_series.append({
                        "participant_id": str(row.participant_id),
                        "local_date": row.local_date.isoformat(),
                        "time": point.get("time"),
                        "workload": workload,
                        "workload_raw": point.get("workload_raw"),
                        "forecast_stress": stress,
                        "continuous_work_hours": point.get("continuous_work_hours"),
                    })
            for event in list((row.output_json or {}).get("classified_calendar_events") or []):
                item = {
                    "participant_id": str(row.participant_id),
                    "local_date": row.local_date.isoformat(),
                    "event_id": event.get("id"),
                    "summary": event.get("summary"),
                    "event_type": event.get("event_type"),
                    "workload_prior": event.get("workload_prior"),
                    "start_time": event.get("start_time"),
                    "end_time": event.get("end_time"),
                }
                events.append(item)
                if str(event.get("event_type") or "").lower() in {
                    "rest", "meal", "nap", "sleep"
                }:
                    recovery_windows.append(item)

        def nearest_workload(
            forecast: dict[str, Any], timestamp: datetime
        ) -> tuple[float, float, str] | None:
            match = self._nearest_point(forecast, _aware(timestamp))
            if match is None:
                return None
            point, point_time = match
            try:
                workload = float(point["workload"])
                stress = float(point["stress_0_10"])
            except (KeyError, TypeError, ValueError):
                return None
            if not (0.0 <= workload <= 1.0 and 0.0 <= stress <= 10.0):
                return None
            return workload, stress, point_time.isoformat()

        actual_series: list[dict[str, Any]] = []
        lag_pairs: dict[int, list[tuple[float, float]]] = {
            lag: [] for lag in (0, 5, 10, 15, 30, 60)
        }
        residual_bins: dict[str, list[float]] = defaultdict(list)
        for observation in observations:
            actual = _score(dict(observation.payload_json or {}), "stress_0_10")
            if actual is None:
                continue
            observed_at = _aware(observation.observed_at)
            created_at = _aware(observation.created_at)
            local_day = observed_at.astimezone(self.timezone).date()
            causal_forecast = self.forecasts.current_at(
                observation.participant_id,
                local_day,
                min(observed_at, created_at),
            )
            if causal_forecast is None:
                continue
            current = nearest_workload(causal_forecast, observed_at)
            if current is not None:
                workload, predicted, point_time = current
                actual_series.append({
                    "participant_id": str(observation.participant_id),
                    "observed_at": _aware(observation.observed_at).isoformat(),
                    "forecast_point_time": point_time,
                    "local_date": _aware(observation.observed_at).astimezone(self.timezone).date().isoformat(),
                    "time": datetime.fromisoformat(point_time).astimezone(self.timezone).strftime("%H:%M"),
                    "workload": workload,
                    "forecast_stress": predicted,
                    "actual_stress": actual,
                    "residual": actual - predicted,
                    "source_forecast_id": causal_forecast["id"],
                    "source_forecast_version": causal_forecast["forecast_version"],
                    "source_semantic_revision": causal_forecast["semantic_revision"],
                })
                start = min(int(workload * 5), 4) * 0.2
                label = f"{start:.1f}–{start + 0.2:.1f}"
                residual_bins[label].append(actual - predicted)
            for lag in lag_pairs:
                lagged = nearest_workload(
                    causal_forecast,
                    observed_at - timedelta(minutes=lag),
                )
                if lagged is not None:
                    lag_pairs[lag].append((lagged[0], actual))

        appraisal_rows = []
        grouped: dict[str, dict[tuple[str, str], list[float]]] = {
            "event_type": defaultdict(list),
            "course": defaultdict(list),
            "participant": defaultdict(list),
        }
        calibration_rows: dict[str, tuple[list[dict[str, Any]], list[float]]] = {}
        for row in appraisals:
            model_version = row.workload_model_version or "unknown"
            if row.workload_residual is not None:
                grouped["event_type"][(model_version, row.event_type or "unknown")].append(row.workload_residual)
                grouped["course"][(model_version, row.course_name or "unknown")].append(row.workload_residual)
                grouped["participant"][(model_version, str(row.participant_id))].append(row.workload_residual)
            if row.workload_feature_vector and row.observed_workload is not None:
                features, observed_values = calibration_rows.setdefault(
                    model_version, ([], [])
                )
                features.append(dict(row.workload_feature_vector))
                observed_values.append(float(row.observed_workload))
            appraisal_rows.append({
                "event_id": row.event_id,
                "participant_id": str(row.participant_id),
                "event_type": row.event_type,
                "course_name": row.course_name,
                "workload_prior": row.workload_prior,
                "observed_workload": row.observed_workload,
                "workload_residual": row.workload_residual,
                "actual_stress": row.actual_stress,
                "workload_model_version": row.workload_model_version,
                "source_forecast_id": (
                    str(row.source_forecast_id) if row.source_forecast_id else None
                ),
            })

        calibration_by_version = []
        for model_version, (feature_rows, observed_rows) in sorted(calibration_rows.items()):
            if len(feature_rows) < 10:
                calibration_by_version.append({
                    "workload_model_version": model_version,
                    "status": "insufficient_sample",
                    "sample_count": len(feature_rows),
                })
                continue
            _, fit = WorkloadEstimator.fit_ridge(feature_rows, observed_rows, alpha=1.0)
            calibration_by_version.append({
                "workload_model_version": model_version,
                "status": "exploratory",
                **fit.to_dict(),
            })
        calibration = (
            {"status": "insufficient_sample", "sample_count": 0}
            if not calibration_by_version
            else calibration_by_version[0]
            if len(calibration_by_version) == 1
            else {
                "status": "separated_by_workload_model_version",
                "model_version_count": len(calibration_by_version),
            }
        )
        actual_pairs = lag_pairs[0]
        if len(actual_pairs) >= 2:
            xs, ys = [x for x, _ in actual_pairs], [y for _, y in actual_pairs]
            x_mean, y_mean = mean(xs), mean(ys)
            variance = sum((x - x_mean) ** 2 for x in xs)
            beta = (
                sum((x - x_mean) * (y - y_mean) for x, y in actual_pairs) / variance
                if variance > 1e-12 else None
            )
            exploratory = {
                "intercept": (y_mean - beta * x_mean) if beta is not None else None,
                "beta_workload": beta,
                "sample_count": len(actual_pairs),
            }
        else:
            exploratory = {"intercept": None, "beta_workload": None, "sample_count": len(actual_pairs)}

        return {
            "date_start": date_start.isoformat(),
            "date_end": date_end.isoformat(),
            "participant_id": str(participant_id) if participant_id else None,
            "series_mode": "latest_descriptive",
            "series": forecast_series,
            "actual_ema": actual_series,
            "calendar_events": events,
            "recovery_windows": recovery_windows,
            "statistics": {
                "corr_workload_forecast_stress": self._correlation(forecast_pairs),
                "corr_workload_actual_stress": self._correlation(actual_pairs),
                "lagged_corr": [
                    {
                        "lag_minutes": lag,
                        "correlation": self._correlation(pairs),
                        "sample_count": len(pairs),
                    }
                    for lag, pairs in lag_pairs.items()
                ],
                "mae_by_workload_bin": [
                    {
                        "bin": label,
                        "mae": mean(abs(value) for value in values),
                        "mean_residual": mean(values),
                        "sample_count": len(values),
                    }
                    for label, values in sorted(residual_bins.items())
                ],
                "exploratory_model": exploratory,
            },
            "event_appraisal": {
                "items": appraisal_rows,
                "ridge_fit": calibration,
                "ridge_fit_by_model_version": calibration_by_version,
                "residual_by": {
                    dimension: [
                        {
                            "workload_model_version": key[0],
                            "group": key[1],
                            "mean_residual": mean(values),
                            "sample_count": len(values),
                        }
                        for key, values in sorted(groups.items())
                    ]
                    for dimension, groups in grouped.items()
                },
            },
        }

    def eligible_participant_days(
        self,
        participants: list[Participant],
        date_start: date,
        date_end: date,
    ) -> set[tuple[uuid.UUID, date]]:
        """Participant-day exposure, excluding dates before enrollment."""
        self._bounds(date_start, date_end)
        result: set[tuple[uuid.UUID, date]] = set()
        for participant in participants:
            joined = _aware(participant.created_at).astimezone(self.timezone).date()
            eligible_start = max(date_start, joined)
            if eligible_start <= date_end:
                for offset in range((date_end - eligible_start).days + 1):
                    result.add((participant.id, eligible_start + timedelta(days=offset)))
        return result

    @staticmethod
    def _point_time(
        local_date: date, raw: Any, timezone_value: ZoneInfo
    ) -> datetime | None:
        try:
            hour, minute = (int(part) for part in str(raw or "")[:5].split(":"))
        except (TypeError, ValueError):
            return None
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        return datetime.combine(
            local_date, time(hour, minute), timezone_value
        ).astimezone(timezone.utc)

    def _nearest_point(
        self, forecast: dict[str, Any], observed_at: datetime
    ) -> tuple[dict[str, Any], datetime] | None:
        local_date = forecast["local_date"]
        if isinstance(local_date, str):
            local_date = date.fromisoformat(local_date)
        candidates = []
        for point in forecast.get("curve") or []:
            timestamp = self._point_time(local_date, point.get("time"), self.timezone)
            if timestamp is not None:
                candidates.append(
                    (abs((timestamp - observed_at).total_seconds()), point, timestamp)
                )
        if not candidates:
            return None
        distance, point, timestamp = min(candidates, key=lambda item: item[0])
        return (point, timestamp) if distance <= MATCH_TOLERANCE_SECONDS else None

    @staticmethod
    def _event_context(
        forecast: dict[str, Any], timestamp: datetime
    ) -> dict[str, Any]:
        event_types: set[str] = set()
        courses: set[str] = set()
        events = (
            (forecast.get("output") or {}).get("classified_calendar_events")
            or []
        )
        for event in events:
            try:
                start = _aware(
                    datetime.fromisoformat(
                        str(event.get("start_time")).replace("Z", "+00:00")
                    )
                )
                end = _aware(
                    datetime.fromisoformat(
                        str(event.get("end_time")).replace("Z", "+00:00")
                    )
                )
            except (TypeError, ValueError):
                continue
            if start <= timestamp < end:
                event_types.add(str(event.get("event_type") or "unknown"))
                course = (
                    event.get("related_course_name")
                    or event.get("related_course_code")
                    or event.get("course_name")
                    or event.get("course_code")
                )
                if course:
                    courses.add(str(course))
        return {"event_types": sorted(event_types), "courses": sorted(courses)}

    def _match_values(
        self, observation: StateObservation, forecast: dict[str, Any]
    ) -> dict[str, Any] | None:
        if observation.observation_type not in MOMENTARY_OBSERVATION_TYPES:
            return None
        payload = dict(observation.payload_json or {})
        actual = _score(payload, "stress_0_10")
        observed_at = _aware(observation.observed_at)
        nearest = self._nearest_point(forecast, observed_at)
        if actual is None or nearest is None:
            return None
        point, forecast_timestamp = nearest
        predicted = _score(point, "stress_0_10")
        if predicted is None:
            return None
        interval = point.get("stress_interval_90_0_10") or {}
        peak = max(
            forecast.get("curve") or [],
            key=lambda item: float(item.get("stress_0_10") or -1),
            default={},
        )
        local_date = observed_at.astimezone(self.timezone).date()
        context = {
            **self._event_context(forecast, forecast_timestamp),
            "time_of_day": observed_at.astimezone(self.timezone).strftime("%H:%M"),
            "forecast_point_time": forecast_timestamp.astimezone(
                self.timezone
            ).strftime("%H:%M"),
            "weekday": observed_at.astimezone(self.timezone).strftime("%A"),
            "workload_0_10": _score(payload, "current_workload_0_10"),
            "algorithm_version": forecast.get("algorithm_version"),
            "engine_version": (forecast.get("output") or {}).get(
                "engine_version", forecast.get("algorithm_version")
            ),
            "model_family": (forecast.get("output") or {}).get("model_family"),
            "model_variant": (forecast.get("output") or {}).get("model_variant"),
            "model_spec_version": (forecast.get("output") or {}).get(
                "model_spec_version"
            ),
            "promotion_decision_id": (forecast.get("output") or {}).get(
                "promotion_decision_id"
            ),
            "promotion_parameters_hash": (forecast.get("output") or {}).get(
                "promotion_parameters_hash"
            ),
            "forecast_peak_stress": _score(peak, "stress_0_10"),
            "forecast_peak_time": peak.get("time"),
        }
        return {
            "participant_id": str(observation.participant_id),
            "local_date": local_date.isoformat(),
            "forecast_id": str(forecast["id"]),
            "forecast_version": forecast["forecast_version"],
            "match_schema_version": MATCH_SCHEMA_VERSION,
            "forecast_timestamp": forecast_timestamp.isoformat(),
            "observation_id": str(observation.id),
            "observed_at": observed_at.isoformat(),
            "predicted_stress": predicted,
            "actual_stress": actual,
            "residual": actual - predicted,
            "prediction_lower": _score(interval, "lower"),
            "prediction_upper": _score(interval, "upper"),
            "context": context,
        }

    def rebuild_matches(
        self,
        *,
        date_start: date,
        date_end: date,
        participant_id: uuid.UUID | None = None,
        observation_cutoff: datetime | None = None,
    ) -> dict[str, Any]:
        lower, upper = self._bounds(date_start, date_end)
        cutoff = _aware(observation_cutoff or datetime.now(timezone.utc))
        conditions = [
            StateObservation.observed_at >= lower,
            StateObservation.observed_at < upper,
            StateObservation.created_at <= cutoff,
            StateObservation.observation_type.in_(MOMENTARY_OBSERVATION_TYPES),
        ]
        if participant_id is not None:
            conditions.append(StateObservation.participant_id == participant_id)
        with self.database.session() as session:
            observations = session.execute(
                select(StateObservation)
                .where(*conditions)
                .order_by(StateObservation.observed_at)
            ).scalars().all()
        created = updated = unmatched = 0
        for observation in observations:
            observed_at = _aware(observation.observed_at)
            local_date = observed_at.astimezone(self.timezone).date()
            causal_cutoff = min(observed_at, _aware(observation.created_at), cutoff)
            forecast = self.forecasts.current_at(
                observation.participant_id, local_date, causal_cutoff
            )
            values = (
                self._match_values(observation, forecast)
                if forecast is not None
                else None
            )
            if values is None:
                unmatched += 1
                continue
            persisted = {
                "participant_id": observation.participant_id,
                "local_date": local_date,
                "forecast_id": uuid.UUID(values["forecast_id"]),
                "forecast_version": values["forecast_version"],
                "match_schema_version": MATCH_SCHEMA_VERSION,
                "forecast_timestamp": datetime.fromisoformat(
                    values["forecast_timestamp"]
                ),
                "observation_id": observation.id,
                "observed_at": observed_at,
                "predicted_stress": values["predicted_stress"],
                "actual_stress": values["actual_stress"],
                "residual": values["residual"],
                "prediction_lower": values["prediction_lower"],
                "prediction_upper": values["prediction_upper"],
                "context_json": values["context"],
            }
            with self.database.session() as session:
                row = session.execute(
                    select(ForecastObservationMatch).where(
                        ForecastObservationMatch.observation_id == observation.id,
                        ForecastObservationMatch.match_schema_version
                        == MATCH_SCHEMA_VERSION,
                    )
                ).scalar_one_or_none()
                if row is None:
                    session.add(ForecastObservationMatch(**persisted))
                    created += 1
                else:
                    for key, value in persisted.items():
                        setattr(row, key, value)
                    updated += 1
        return {
            "created": created,
            "updated": updated,
            "unmatched": unmatched,
            "examined": len(observations),
            "match_schema_version": MATCH_SCHEMA_VERSION,
        }

    @staticmethod
    def _match_view(row: ForecastObservationMatch) -> dict[str, Any]:
        return {
            "id": str(row.id),
            "participant_id": str(row.participant_id),
            "local_date": row.local_date.isoformat(),
            "forecast_id": str(row.forecast_id),
            "forecast_version": row.forecast_version,
            "match_schema_version": row.match_schema_version,
            "forecast_timestamp": _iso(row.forecast_timestamp),
            "observation_id": str(row.observation_id),
            "observed_at": _iso(row.observed_at),
            "predicted_stress": row.predicted_stress,
            "actual_stress": row.actual_stress,
            "residual": row.residual,
            "prediction_lower": row.prediction_lower,
            "prediction_upper": row.prediction_upper,
            "context": dict(row.context_json or {}),
        }

    def matches(
        self,
        date_start: date,
        date_end: date,
        participant_id: uuid.UUID | None = None,
        *,
        observation_cutoff: datetime | None = None,
    ) -> list[dict[str, Any]]:
        conditions = [
            ForecastObservationMatch.local_date >= date_start,
            ForecastObservationMatch.local_date <= date_end,
            ForecastObservationMatch.match_schema_version == MATCH_SCHEMA_VERSION,
            StateObservation.observation_type.in_(MOMENTARY_OBSERVATION_TYPES),
        ]
        if participant_id is not None:
            conditions.append(
                ForecastObservationMatch.participant_id == participant_id
            )
        statement = select(ForecastObservationMatch).join(
            StateObservation,
            StateObservation.id == ForecastObservationMatch.observation_id,
        )
        if observation_cutoff is not None:
            conditions.append(
                StateObservation.created_at <= _aware(observation_cutoff)
            )
        with self.database.session() as session:
            rows = session.execute(
                statement.where(*conditions).order_by(
                    ForecastObservationMatch.observed_at
                )
            ).scalars().all()
            return [self._match_view(row) for row in rows]

    @staticmethod
    def metrics(matches: list[dict[str, Any]]) -> dict[str, Any]:
        result = {
            "sample_count": len(matches),
            "mae": None,
            "rmse": None,
            "median_absolute_error": None,
            "interval_nominal_coverage": 0.9,
            "interval_90_coverage": None,
            "mean_interval_width": None,
            "observed_peak_proxy_magnitude_error": None,
            "observed_peak_proxy_timing_error_minutes": None,
            "peak_proxy_day_count": 0,
            "peak_proxy_mean_samples_per_day": None,
        }
        if not matches:
            return result
        residuals = [float(item["residual"]) for item in matches]
        intervals = [
            item
            for item in matches
            if item["prediction_lower"] is not None
            and item["prediction_upper"] is not None
        ]
        groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(
            list
        )
        for item in matches:
            groups[
                (
                    item["participant_id"],
                    item["local_date"],
                    item["forecast_version"],
                )
            ].append(item)
        peak_groups = [items for items in groups.values() if len(items) >= 2]
        magnitude_errors: list[float] = []
        timing_errors: list[float] = []
        for items in peak_groups:
            actual_peak = max(items, key=lambda item: item["actual_stress"])
            context = actual_peak["context"]
            if context.get("forecast_peak_stress") is not None:
                magnitude_errors.append(
                    abs(
                        actual_peak["actual_stress"]
                        - float(context["forecast_peak_stress"])
                    )
                )
            if context.get("forecast_peak_time"):
                try:
                    ah, am = map(int, context["time_of_day"][:5].split(":"))
                    ph, pm = map(
                        int, str(context["forecast_peak_time"])[:5].split(":")
                    )
                    timing_errors.append(abs((ah * 60 + am) - (ph * 60 + pm)))
                except (TypeError, ValueError):
                    pass
        coverage = [
            item["prediction_lower"]
            <= item["actual_stress"]
            <= item["prediction_upper"]
            for item in intervals
        ]
        result.update(
            {
                "mae": round(mean(abs(value) for value in residuals), 4),
                "rmse": round(
                    math.sqrt(mean(value * value for value in residuals)), 4
                ),
                "median_absolute_error": round(
                    median(abs(value) for value in residuals), 4
                ),
                "interval_90_coverage": round(mean(coverage), 4)
                if coverage
                else None,
                "mean_interval_width": round(
                    mean(
                        item["prediction_upper"] - item["prediction_lower"]
                        for item in intervals
                    ),
                    4,
                )
                if intervals
                else None,
                "observed_peak_proxy_magnitude_error": round(
                    mean(magnitude_errors), 4
                )
                if magnitude_errors
                else None,
                "observed_peak_proxy_timing_error_minutes": round(
                    mean(timing_errors), 2
                )
                if timing_errors
                else None,
                "peak_proxy_day_count": len(peak_groups),
                "peak_proxy_mean_samples_per_day": round(
                    mean(len(items) for items in peak_groups), 2
                )
                if peak_groups
                else None,
            }
        )
        return result

    @staticmethod
    def residual_diagnostics(
        matches: list[dict[str, Any]],
    ) -> dict[str, list[dict[str, Any]]]:
        dimensions: dict[str, dict[str, list[float]]] = {
            key: defaultdict(list)
            for key in (
                "time_of_day",
                "workload",
                "event_type",
                "course",
                "weekday",
            )
        }
        for item in matches:
            residual, context = float(item["residual"]), item["context"]
            hour = int(str(context.get("time_of_day") or "00")[:2])
            bucket = (
                "00:00–06:00"
                if hour < 6
                else "06:00–10:00"
                if hour < 10
                else "10:00–14:00"
                if hour < 14
                else "14:00–18:00"
                if hour < 18
                else "18:00–24:00"
            )
            dimensions["time_of_day"][bucket].append(residual)
            workload = context.get("workload_0_10")
            if workload is not None:
                start = min(int(float(workload)), 9) // 2 * 2
                dimensions["workload"][f"{start}–{start + 2}"].append(residual)
            for value in context.get("event_types") or ["none"]:
                dimensions["event_type"][str(value)].append(residual)
            for value in context.get("courses") or ["none"]:
                dimensions["course"][str(value)].append(residual)
            dimensions["weekday"][
                str(context.get("weekday") or "unknown")
            ].append(residual)
        return {
            dimension: [
                {
                    "group": group,
                    "mean_residual": round(mean(values), 4),
                    "median_residual": round(median(values), 4),
                    "mae": round(mean(abs(value) for value in values), 4),
                    "sample_count": len(values),
                }
                for group, values in sorted(groups.items())
            ]
            for dimension, groups in dimensions.items()
        }

    def evaluation(
        self,
        date_start: date,
        date_end: date,
        participant_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        matches = self.matches(date_start, date_end, participant_id)
        return {
            "date_start": date_start.isoformat(),
            "date_end": date_end.isoformat(),
            "metrics": self.metrics(matches),
            "residual_diagnostics": self.residual_diagnostics(matches),
            "matches": matches,
        }

    def _currentness_event(
        self,
        participant_id: uuid.UUID,
        local_date: date,
        cutoff: datetime,
        forecast_id: uuid.UUID,
    ) -> ForecastCurrentnessEvent | None:
        with self.database.session() as session:
            event = session.execute(
                select(ForecastCurrentnessEvent)
                .where(
                    ForecastCurrentnessEvent.participant_id == participant_id,
                    ForecastCurrentnessEvent.local_date == local_date,
                    ForecastCurrentnessEvent.occurred_at <= cutoff,
                )
                .order_by(
                    desc(ForecastCurrentnessEvent.occurred_at),
                    desc(ForecastCurrentnessEvent.id),
                )
                .limit(1)
            ).scalar_one_or_none()
            if (
                event is None
                or event.event_type != "activated"
                or event.forecast_id != forecast_id
            ):
                return None
            return event

    @staticmethod
    def _item(
        item_type: str,
        source_id: str,
        source_version: str,
        participant_id: uuid.UUID,
        local_date: date,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "item_type": item_type,
            "source_id": str(source_id),
            "source_version": str(source_version),
            "participant_id": participant_id,
            "local_date": local_date,
            "source_hash": _hash(metadata),
            "metadata": metadata,
        }

    @staticmethod
    def _manifest_hash(
        contract: dict[str, Any], items: list[dict[str, Any]]
    ) -> str:
        canonical_items = sorted(
            [
                {
                    "item_type": item["item_type"],
                    "source_id": item["source_id"],
                    "source_version": item["source_version"],
                    "participant_id": str(item["participant_id"]),
                    "local_date": item["local_date"].isoformat()
                    if isinstance(item["local_date"], date)
                    else str(item["local_date"]),
                    "source_hash": item["source_hash"],
                    "metadata": item["metadata"],
                }
                for item in items
            ],
            key=lambda item: (
                item["item_type"],
                item["source_id"],
                item["source_version"],
            ),
        )
        return _hash({"contract": contract, "items": canonical_items})

    def create_dataset_snapshot(
        self,
        *,
        date_start: date,
        date_end: date,
        participant_filter: dict[str, Any] | None = None,
        observation_cutoff: datetime | None = None,
        calendar_cutoff: datetime | None = None,
    ) -> dict[str, Any]:
        lower, upper = self._bounds(date_start, date_end)
        requested = dict(participant_filter or {})
        if set(requested) - {"participant_codes"}:
            raise ValueError("unsupported participant filter")
        raw_codes = requested.get("participant_codes") or []
        if not isinstance(raw_codes, (list, tuple)):
            raise ValueError("participant_codes must be an array")
        codes = sorted({str(code).strip() for code in raw_codes if str(code).strip()})
        observation_cutoff = _aware(
            observation_cutoff or datetime.now(timezone.utc)
        )
        calendar_cutoff = _aware(calendar_cutoff or observation_cutoff)
        with self.database.session() as session:
            statement = select(Participant)
            if codes:
                statement = statement.where(Participant.participant_code.in_(codes))
            participants = session.execute(statement).scalars().all()
            unknown = sorted(set(codes) - {row.participant_code for row in participants})
            if unknown:
                raise ValueError(f"unknown participant_codes: {', '.join(unknown)}")
            if not participants:
                raise ValueError("dataset snapshot requires at least one participant")
            participant_ids = [row.id for row in participants]
            observations = session.execute(
                select(StateObservation)
                .where(
                    StateObservation.participant_id.in_(participant_ids),
                    StateObservation.observed_at >= lower,
                    StateObservation.observed_at < upper,
                    StateObservation.created_at <= observation_cutoff,
                    StateObservation.observation_type.in_(
                        MOMENTARY_OBSERVATION_TYPES
                    ),
                )
                .order_by(
                    StateObservation.participant_id,
                    StateObservation.observed_at,
                    StateObservation.id,
                )
            ).scalars().all()
            psychometrics = session.execute(
                select(PsychometricAssessment)
                .where(
                    PsychometricAssessment.participant_id.in_(participant_ids),
                    PsychometricAssessment.administered_at < upper,
                    PsychometricAssessment.created_at <= observation_cutoff,
                )
                .order_by(
                    PsychometricAssessment.participant_id,
                    PsychometricAssessment.administered_at,
                )
            ).scalars().all()
            daily_reviews = session.execute(
                select(DailyReviewResponse)
                .where(
                    DailyReviewResponse.participant_id.in_(participant_ids),
                    DailyReviewResponse.local_date >= date_start,
                    DailyReviewResponse.local_date <= date_end,
                    DailyReviewResponse.submitted_at <= observation_cutoff,
                )
                .order_by(
                    DailyReviewResponse.participant_id,
                    DailyReviewResponse.local_date,
                    DailyReviewResponse.submitted_at,
                )
            ).scalars().all()
            slow_states = session.execute(
                select(ParticipantSlowState)
                .where(
                    ParticipantSlowState.participant_id.in_(participant_ids),
                    ParticipantSlowState.effective_at >= lower,
                    ParticipantSlowState.effective_at < upper,
                    ParticipantSlowState.created_at <= observation_cutoff,
                )
                .order_by(
                    ParticipantSlowState.participant_id,
                    ParticipantSlowState.effective_at,
                )
            ).scalars().all()
            exposure_lower = lower - timedelta(
                minutes=STAGE5_INTERVENTION_EXCLUSION_MINUTES
            )
            warning_deliveries = session.execute(
                select(WarningSchedule)
                .where(
                    WarningSchedule.participant_id.in_(participant_ids),
                    WarningSchedule.sent_at.is_not(None),
                    WarningSchedule.sent_at >= exposure_lower,
                    WarningSchedule.sent_at < upper,
                    WarningSchedule.sent_at <= observation_cutoff,
                    WarningSchedule.authorized_at.is_not(None),
                    WarningSchedule.authorized_at <= observation_cutoff,
                )
                .order_by(
                    WarningSchedule.participant_id,
                    WarningSchedule.sent_at,
                    WarningSchedule.id,
                )
            ).scalars().all()
            care_exposures = session.execute(
                select(CareInterventionEvent)
                .where(
                    CareInterventionEvent.participant_id.in_(participant_ids),
                    CareInterventionEvent.sent_at.is_not(None),
                    CareInterventionEvent.sent_at >= exposure_lower,
                    CareInterventionEvent.sent_at < upper,
                    CareInterventionEvent.sent_at <= observation_cutoff,
                    CareInterventionEvent.created_at <= observation_cutoff,
                )
                .order_by(
                    CareInterventionEvent.participant_id,
                    CareInterventionEvent.sent_at,
                    CareInterventionEvent.id,
                )
            ).scalars().all()
        item_map: dict[tuple[str, str, str], dict[str, Any]] = {}

        def freeze(item: dict[str, Any]) -> None:
            key = (item["item_type"], item["source_id"], item["source_version"])
            if key in item_map and item_map[key]["source_hash"] != item["source_hash"]:
                raise ValueError(f"conflicting immutable source: {key}")
            item_map[key] = item

        for participant in participants:
            membership_metadata = {
                "participant_id": str(participant.id),
                "participant_code": participant.participant_code,
                "joined_at": _aware(participant.created_at).isoformat(),
                "status_at_snapshot": participant.status,
            }
            freeze(
                self._item(
                    "participant",
                    str(participant.id),
                    "participant-membership.v1",
                    participant.id,
                    date_start,
                    membership_metadata,
                )
            )

        for assessment in psychometrics:
            administered = _aware(assessment.administered_at)
            freeze(
                self._item(
                    "psychometric",
                    str(assessment.id),
                    f"{assessment.instrument_name}.{assessment.instrument_version}",
                    assessment.participant_id,
                    administered.astimezone(self.timezone).date(),
                    {
                        "assessment_id": str(assessment.id),
                        "instrument_name": assessment.instrument_name,
                        "instrument_version": assessment.instrument_version,
                        "language": assessment.language,
                        "scores": dict(assessment.scores_json or {}),
                        "administered_at": administered.isoformat(),
                        "created_at": _aware(assessment.created_at).isoformat(),
                    },
                )
            )

        for review in daily_reviews:
            freeze(
                self._item(
                    "daily_review",
                    str(review.id),
                    "daily-review-recovery.v1",
                    review.participant_id,
                    review.local_date,
                    {
                        "daily_review_id": str(review.id),
                        "local_date": review.local_date.isoformat(),
                        "start_stress": review.start_stress,
                        "end_stress": review.end_stress,
                        "start_energy": review.start_energy,
                        "end_energy": review.end_energy,
                        "recovery_note": review.recovery_note,
                        "submitted_at": _aware(review.submitted_at).isoformat(),
                    },
                )
            )

        for slow_state in slow_states:
            effective = _aware(slow_state.effective_at)
            freeze(
                self._item(
                    "slow_state",
                    str(slow_state.id),
                    "slow-state-recovery.v1",
                    slow_state.participant_id,
                    effective.astimezone(self.timezone).date(),
                    {
                        "slow_state_id": str(slow_state.id),
                        "effective_at": effective.isoformat(),
                        "recent_recovery_quality": slow_state.recent_recovery_quality,
                        "recent_sleep_debt": slow_state.recent_sleep_debt,
                        "rolling_7d_stress": slow_state.rolling_7d_stress,
                        "rolling_7d_workload": slow_state.rolling_7d_workload,
                        "source": slow_state.source,
                        "created_at": _aware(slow_state.created_at).isoformat(),
                    },
                )
            )

        for warning in warning_deliveries:
            sent_at = _aware(warning.sent_at)
            freeze(
                self._item(
                    "warning_delivery",
                    str(warning.id),
                    "warning-delivery-exposure.v1",
                    warning.participant_id,
                    sent_at.astimezone(self.timezone).date(),
                    {
                        "warning_id": str(warning.id),
                        "forecast_id": str(warning.forecast_id),
                        "forecast_version": warning.forecast_version,
                        "warning_level": warning.warning_level,
                        "authorized_at": _aware(warning.authorized_at).isoformat(),
                        "sent_at": sent_at.isoformat(),
                        "intervention_type": "warning",
                    },
                )
            )

        for intervention in care_exposures:
            sent_at = _aware(intervention.sent_at)
            freeze(
                self._item(
                    "care_intervention_exposure",
                    str(intervention.id),
                    "care-intervention-exposure.v1",
                    intervention.participant_id,
                    sent_at.astimezone(self.timezone).date(),
                    {
                        "intervention_id": str(intervention.id),
                        "source_warning_id": str(intervention.source_warning_id),
                        "source_forecast_id": str(intervention.source_forecast_id),
                        "forecast_version": intervention.forecast_version,
                        "intervention_type": intervention.intervention_type,
                        "scheduled_at": _aware(
                            intervention.scheduled_at
                        ).isoformat(),
                        "sent_at": sent_at.isoformat(),
                        "created_at": _aware(intervention.created_at).isoformat(),
                    },
                )
            )

        for observation in observations:
            observed_at = _aware(observation.observed_at)
            created_at = _aware(observation.created_at)
            local_date = observed_at.astimezone(self.timezone).date()
            observation_metadata = {
                "observation_id": str(observation.id),
                "observation_type": observation.observation_type,
                "source_message_id": observation.source_message_id,
                "observed_at": observed_at.isoformat(),
                "created_at": created_at.isoformat(),
                "payload": dict(observation.payload_json or {}),
            }
            freeze(
                self._item(
                    "observation",
                    str(observation.id),
                    "observation.v1",
                    observation.participant_id,
                    local_date,
                    observation_metadata,
                )
            )
            causal_cutoff = min(observed_at, created_at, observation_cutoff)
            forecast = self.forecasts.current_at(
                observation.participant_id, local_date, causal_cutoff
            )
            if forecast is None:
                continue
            generated_at = _aware(datetime.fromisoformat(forecast["generated_at"]))
            # The embedded calendar representation cannot be newer than the
            # explicit dataset calendar knowledge cutoff.
            if generated_at > calendar_cutoff:
                continue
            forecast_id = uuid.UUID(str(forecast["id"]))
            currentness = self._currentness_event(
                observation.participant_id,
                local_date,
                causal_cutoff,
                forecast_id,
            )
            if currentness is None:
                continue
            forecast_metadata = {
                "forecast_id": str(forecast_id),
                "forecast_version": forecast["forecast_version"],
                "algorithm_version": forecast["algorithm_version"],
                "engine_version": (forecast.get("output") or {}).get(
                    "engine_version", forecast["algorithm_version"]
                ),
                "generated_at": forecast["generated_at"],
                "calendar_revision": forecast["calendar_revision"],
                "semantic_revision": forecast["semantic_revision"],
                "observation_revision": forecast["observation_revision"],
                "model_family": (forecast.get("output") or {}).get(
                    "model_family"
                ),
                "model_variant": (forecast.get("output") or {}).get(
                    "model_variant"
                ),
                "model_spec_version": (forecast.get("output") or {}).get(
                    "model_spec_version"
                ),
                "promotion_decision_id": (forecast.get("output") or {}).get(
                    "promotion_decision_id"
                ),
                "promotion_parameters_hash": (forecast.get("output") or {}).get(
                    "promotion_parameters_hash"
                ),
                "initial_state": dict(
                    (forecast.get("output") or {}).get("initial_state") or {}
                ),
                "initial_state_revision": (forecast.get("output") or {}).get(
                    "initial_state_revision"
                ),
                "curve": list(forecast.get("curve") or []),
                "peaks": list(forecast.get("peaks") or []),
            }
            freeze(
                self._item(
                    "forecast",
                    str(forecast_id),
                    forecast["forecast_version"],
                    observation.participant_id,
                    local_date,
                    forecast_metadata,
                )
            )
            currentness_metadata = {
                "event_id": currentness.id,
                "event_type": currentness.event_type,
                "forecast_id": str(currentness.forecast_id),
                "forecast_version": currentness.forecast_version,
                "occurred_at": _iso(currentness.occurred_at),
                "reason": currentness.reason,
            }
            freeze(
                self._item(
                    "forecast_currentness",
                    str(currentness.id),
                    currentness.event_type,
                    observation.participant_id,
                    local_date,
                    currentness_metadata,
                )
            )
            calendar_metadata = {
                "provenance_source": "forecast_snapshot",
                "forecast_id": str(forecast_id),
                "calendar_revision": forecast["calendar_revision"],
                "calendar_representation": list(
                    (forecast.get("output") or {}).get(
                        "classified_calendar_events"
                    )
                    or []
                ),
                "knowledge_upper_bound": forecast["generated_at"],
            }
            freeze(
                self._item(
                    "calendar",
                    str(forecast_id),
                    forecast["calendar_revision"],
                    observation.participant_id,
                    local_date,
                    calendar_metadata,
                )
            )
            match = self._match_values(observation, forecast)
            if match is not None:
                match["currentness_event_id"] = currentness.id
                freeze(
                    self._item(
                        "match_source",
                        str(observation.id),
                        MATCH_SCHEMA_VERSION,
                        observation.participant_id,
                        local_date,
                        match,
                    )
                )
        items = list(item_map.values())
        participant_filter = {"participant_codes": codes}
        contract = {
            "schema_version": DATASET_SCHEMA_VERSION,
            "date_start": date_start.isoformat(),
            "date_end": date_end.isoformat(),
            "participant_filter": participant_filter,
            "observation_cutoff": observation_cutoff.isoformat(),
            "calendar_cutoff": calendar_cutoff.isoformat(),
        }
        manifest_hash = self._manifest_hash(contract, items)
        type_count = lambda kind: sum(item["item_type"] == kind for item in items)
        manifest = {
            "schema_version": DATASET_SCHEMA_VERSION,
            "participant_count": type_count("participant"),
            "observation_count": type_count("observation"),
            "forecast_count": type_count("forecast"),
            "calendar_count": type_count("calendar"),
            "psychometric_count": type_count("psychometric"),
            "daily_review_count": type_count("daily_review"),
            "slow_state_count": type_count("slow_state"),
            "care_intervention_exposure_count": type_count(
                "care_intervention_exposure"
            ),
            "warning_delivery_count": type_count("warning_delivery"),
            "item_count": len(items),
            "manifest_hash": manifest_hash,
        }
        snapshot_id = uuid.uuid4()
        with self.database.session() as session:
            row = DatasetSnapshot(
                id=snapshot_id,
                date_start=date_start,
                date_end=date_end,
                participant_filter=participant_filter,
                observation_cutoff=observation_cutoff,
                calendar_cutoff=calendar_cutoff,
                schema_version=DATASET_SCHEMA_VERSION,
                manifest_json=manifest,
            )
            session.add(row)
            session.add_all(
                [
                    DatasetSnapshotItem(
                        dataset_snapshot_id=snapshot_id,
                        item_type=item["item_type"],
                        source_id=item["source_id"],
                        source_version=item["source_version"],
                        participant_id=item["participant_id"],
                        local_date=item["local_date"],
                        source_hash=item["source_hash"],
                        metadata_json=item["metadata"],
                    )
                    for item in items
                ]
            )
            session.flush()
            return self._snapshot_view(row)

    @staticmethod
    def _snapshot_view(row: DatasetSnapshot) -> dict[str, Any]:
        return {
            "id": str(row.id),
            "created_at": _iso(row.created_at),
            "date_start": row.date_start.isoformat(),
            "date_end": row.date_end.isoformat(),
            "participant_filter": dict(row.participant_filter),
            "observation_cutoff": _aware(row.observation_cutoff).isoformat(),
            "calendar_cutoff": _aware(row.calendar_cutoff).isoformat(),
            "schema_version": row.schema_version,
            "manifest": dict(row.manifest_json),
        }

    @staticmethod
    def _snapshot_item_view(row: DatasetSnapshotItem) -> dict[str, Any]:
        return {
            "id": str(row.id),
            "dataset_snapshot_id": str(row.dataset_snapshot_id),
            "item_type": row.item_type,
            "source_id": row.source_id,
            "source_version": row.source_version,
            "participant_id": str(row.participant_id),
            "local_date": row.local_date.isoformat(),
            "source_hash": row.source_hash,
            "metadata": dict(row.metadata_json),
            "created_at": _iso(row.created_at),
        }

    def list_snapshots(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.database.session() as session:
            rows = session.execute(
                select(DatasetSnapshot)
                .order_by(desc(DatasetSnapshot.created_at))
                .limit(limit)
            ).scalars().all()
            return [self._snapshot_view(row) for row in rows]

    def snapshot_items(
        self, snapshot_id: uuid.UUID, item_type: str | None = None
    ) -> list[dict[str, Any]]:
        conditions = [DatasetSnapshotItem.dataset_snapshot_id == snapshot_id]
        if item_type is not None:
            conditions.append(DatasetSnapshotItem.item_type == item_type)
        with self.database.session() as session:
            rows = session.execute(
                select(DatasetSnapshotItem)
                .where(*conditions)
                .order_by(
                    DatasetSnapshotItem.item_type,
                    DatasetSnapshotItem.source_id,
                    DatasetSnapshotItem.source_version,
                )
            ).scalars().all()
            return [self._snapshot_item_view(row) for row in rows]

    def create_evaluation_run(
        self,
        snapshot_id: uuid.UUID,
        model_version: str,
        participant_id: uuid.UUID | None = None,
        evaluation_mode: str = "historical_online",
        model_identity_filter: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        model_version = str(model_version).strip()
        if not model_version or len(model_version) > 64:
            raise ValueError("model_version must contain 1 to 64 characters")
        evaluation_mode = str(evaluation_mode).strip()
        if evaluation_mode not in EVALUATION_MODES:
            raise ValueError("unsupported evaluation_mode")
        if model_identity_filter is not None and evaluation_mode != "historical_online":
            raise ValueError(
                "model_identity_filter is only supported for historical_online"
            )
        if model_identity_filter is not None:
            if not isinstance(model_identity_filter, dict):
                raise ValueError("model_identity_filter must be an object")
            unknown_identity_fields = (
                set(model_identity_filter) - MODEL_IDENTITY_FILTER_FIELDS
            )
            if unknown_identity_fields:
                raise ValueError("model_identity_filter contains unsupported fields")
            resolved_identity_filter = {
                key: str(value).strip()
                for key, value in model_identity_filter.items()
                if value is not None and str(value).strip()
            }
            if not resolved_identity_filter:
                raise ValueError("model_identity_filter must not be empty")
        else:
            resolved_identity_filter = None
        with self.database.session() as session:
            snapshot = session.get(DatasetSnapshot, snapshot_id)
            if snapshot is None:
                raise ValueError("dataset snapshot not found")
            rows = session.execute(
                select(DatasetSnapshotItem).where(
                    DatasetSnapshotItem.dataset_snapshot_id == snapshot_id
                )
            ).scalars().all()
            items = [
                {
                    "item_type": row.item_type,
                    "source_id": row.source_id,
                    "source_version": row.source_version,
                    "participant_id": row.participant_id,
                    "local_date": row.local_date,
                    "source_hash": row.source_hash,
                    "metadata": dict(row.metadata_json),
                }
                for row in rows
            ]
            snapshot_view = self._snapshot_view(snapshot)
        if not items:
            raise ValueError("dataset snapshot has no immutable items")
        manifest = snapshot_view["manifest"]
        schema_version = snapshot_view["schema_version"]
        if schema_version not in {
            DATASET_SCHEMA_V2,
            DATASET_SCHEMA_V3,
            DATASET_SCHEMA_V4,
            DATASET_SCHEMA_V5,
        }:
            raise ValueError("unsupported dataset schema version")
        if manifest.get("schema_version") != schema_version:
            raise ValueError("dataset snapshot schema/manifest mismatch")
        expected_counts = {
            "item_count": len(items),
            "observation_count": sum(
                item["item_type"] == "observation" for item in items
            ),
            "forecast_count": sum(
                item["item_type"] == "forecast" for item in items
            ),
            "calendar_count": sum(
                item["item_type"] == "calendar" for item in items
            ),
        }
        participant_item_count = sum(
            item["item_type"] == "participant" for item in items
        )
        if schema_version == DATASET_SCHEMA_V2:
            if participant_item_count:
                raise ValueError(
                    "legacy v2 dataset contains v3 participant membership"
                )
        elif schema_version == DATASET_SCHEMA_V3:
            expected_counts["participant_count"] = participant_item_count
        elif schema_version in {DATASET_SCHEMA_V4, DATASET_SCHEMA_V5}:
            expected_counts["participant_count"] = participant_item_count
            expected_counts.update(
                {
                    "psychometric_count": sum(
                        item["item_type"] == "psychometric" for item in items
                    ),
                    "daily_review_count": sum(
                        item["item_type"] == "daily_review" for item in items
                    ),
                    "slow_state_count": sum(
                        item["item_type"] == "slow_state" for item in items
                    ),
                }
            )
            if schema_version == DATASET_SCHEMA_V5:
                expected_counts.update(
                    {
                        "care_intervention_exposure_count": sum(
                            item["item_type"] == "care_intervention_exposure"
                            for item in items
                        ),
                        "warning_delivery_count": sum(
                            item["item_type"] == "warning_delivery"
                            for item in items
                        ),
                    }
                )
        if any(manifest.get(key) != value for key, value in expected_counts.items()):
            raise ValueError("dataset snapshot manifest/items count mismatch")
        if (
            schema_version in {
                DATASET_SCHEMA_V3,
                DATASET_SCHEMA_V4,
                DATASET_SCHEMA_V5,
            }
            and participant_item_count <= 0
        ):
            raise ValueError("dataset snapshot has no frozen participant membership")
        contract = {
            "schema_version": snapshot_view["schema_version"],
            "date_start": snapshot_view["date_start"],
            "date_end": snapshot_view["date_end"],
            "participant_filter": snapshot_view["participant_filter"],
            "observation_cutoff": snapshot_view["observation_cutoff"],
            "calendar_cutoff": snapshot_view["calendar_cutoff"],
        }
        manifest_hash = self._manifest_hash(contract, items)
        if manifest_hash != manifest.get("manifest_hash"):
            raise ValueError("dataset snapshot manifest mismatch")
        if schema_version in {
            DATASET_SCHEMA_V3,
            DATASET_SCHEMA_V4,
            DATASET_SCHEMA_V5,
        }:
            participant_ids = {
                item["participant_id"]
                for item in items
                if item["item_type"] == "participant"
            }
            if participant_id is not None and participant_id not in participant_ids:
                raise ValueError("participant is outside dataset snapshot")
        else:
            legacy_membership_evidence = {
                item["participant_id"]
                for item in items
                if item["item_type"]
                in {"observation", "forecast", "calendar", "match_source"}
            }
            if (
                participant_id is not None
                and participant_id not in legacy_membership_evidence
            ):
                raise ValueError("legacy_v2_snapshot_membership_unknown")
        all_match_items = [
            item
            for item in items
            if item["item_type"] == "match_source"
        ]
        match_items = [
            item
            for item in all_match_items
            if participant_id is None or item["participant_id"] == participant_id
        ]

        def source_set(source_items: list[dict[str, Any]]) -> dict[str, Any]:
            contexts = [
                dict((item["metadata"].get("context") or {}))
                for item in source_items
            ]
            return {
                "observation_ids": sorted(
                    str(
                        item["metadata"].get("observation_id")
                        or item["source_id"]
                    )
                    for item in source_items
                ),
                "forecast_ids": sorted(
                    {
                        str(item["metadata"].get("forecast_id"))
                        for item in source_items
                        if item["metadata"].get("forecast_id")
                    }
                ),
                "match_source_hashes": sorted(
                    item["source_hash"] for item in source_items
                ),
                "promotion_decision_ids": sorted(
                    {
                        str(context.get("promotion_decision_id"))
                        for context in contexts
                        if context.get("promotion_decision_id")
                    }
                ),
                "promotion_parameters_hashes": sorted(
                    {
                        str(context.get("promotion_parameters_hash"))
                        for context in contexts
                        if context.get("promotion_parameters_hash")
                    }
                ),
            }
        config = {
            "evaluation_mode": evaluation_mode,
            "dataset_schema_version": snapshot_view["schema_version"],
            "manifest_hash": manifest_hash,
            "model_version": model_version,
            "evaluation_code_version": EVALUATION_CODE_VERSION,
            "dataset_snapshot_id": str(snapshot_id),
            "observation_cutoff": snapshot_view["observation_cutoff"],
            "calendar_cutoff": snapshot_view["calendar_cutoff"],
            "snapshot_source_set": source_set(all_match_items),
            "model_identity_filter": (
                dict(resolved_identity_filter)
                if resolved_identity_filter is not None
                else {"legacy_model_version": model_version}
            ),
        }
        if evaluation_mode == "offline_replay":
            requested_family = self._model_family(model_version)
            metrics_json = self._offline_model_comparison(
                items,
                participant_id=participant_id,
                requested_family=requested_family,
                config=config,
            )
            status = "completed"
        else:
            matched_items = []
            for item in match_items:
                metadata = dict(item["metadata"])
                context = dict(metadata.get("context") or {})
                if resolved_identity_filter is not None:
                    matched = all(
                        str(context.get(key) or "") == expected
                        for key, expected in resolved_identity_filter.items()
                    )
                else:
                    matched = model_version in {
                        context.get("algorithm_version"),
                        context.get("engine_version"),
                        context.get("model_spec_version"),
                        context.get("model_family"),
                        context.get("model_variant"),
                    }
                if matched:
                    matched_items.append(item)
            matches = [dict(item["metadata"]) for item in matched_items]
            config["evaluation_source_set"] = source_set(matched_items)
            config["matched_promotion_decision_ids"] = sorted(
                {
                    str((item.get("context") or {}).get("promotion_decision_id"))
                    for item in matches
                    if (item.get("context") or {}).get("promotion_decision_id")
                }
            )
            config["matched_parameters_hashes"] = sorted(
                {
                    str((item.get("context") or {}).get("promotion_parameters_hash"))
                    for item in matches
                    if (item.get("context") or {}).get(
                        "promotion_parameters_hash"
                    )
                }
            )
            metrics_json = {
                "config": config,
                "metrics": self.metrics(matches),
                "residual_diagnostics": self.residual_diagnostics(matches),
                "matched_observation_count": len(matches),
            }
            status = "completed"
        with self.database.session() as session:
            row = ModelEvaluationRun(
                dataset_snapshot_id=snapshot_id,
                model_version=model_version,
                evaluation_mode=evaluation_mode,
                evaluation_code_version=EVALUATION_CODE_VERSION,
                participant_id=participant_id,
                metrics_json=metrics_json,
                status=status,
            )
            session.add(row)
            session.flush()
            return self._run_view(row)

    @staticmethod
    def _model_family(value: str) -> str:
        normalized = str(value).strip().lower().replace("-", "_")
        aliases = {
            "current": "current_m0",
            "m0": "current_m0",
            "stress_ctssm.m0": "current_m0",
            "wm0": "workload_aware_m0",
            "workload_m0": "workload_aware_m0",
            "workload_aware_stress_ctssm.m0": "workload_aware_m0",
            "all": "all",
            "ctssm_family_comparison": "all",
        }
        family = aliases.get(normalized, normalized)
        if family not in set(MODEL_FAMILIES) | {"all"}:
            # Stage-2 clients used arbitrary candidate version labels.  Stage
            # 4 interprets those as a request for the full frozen comparison
            # while retaining the caller's label in evaluation provenance.
            return "all"
        return family

    def _offline_model_comparison(
        self,
        items: list[dict[str, Any]],
        *,
        participant_id: uuid.UUID | None,
        requested_family: str,
        config: dict[str, Any],
    ) -> dict[str, Any]:
        return Stage4CandidateReplayService(self.timezone.key).compare(
            items,
            participant_id=participant_id,
            requested_family=requested_family,
            config=config,
        )
    def _frozen_calendar_recovery(
        calendar: Mapping[str, Any], observed_at: str
    ) -> float:
        """Derive only observable v1 calendar recovery resources."""

        try:
            observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return 0.0
        demanding: list[tuple[datetime, datetime]] = []
        active_recovery = 0.0
        for event in calendar.get("calendar_representation") or []:
            try:
                start = datetime.fromisoformat(
                    str(event.get("start_time") or "").replace("Z", "+00:00")
                )
                end = datetime.fromisoformat(
                    str(event.get("end_time") or "").replace("Z", "+00:00")
                )
                if start.tzinfo is None:
                    start = start.replace(tzinfo=observed.tzinfo)
                if end.tzinfo is None:
                    end = end.replace(tzinfo=observed.tzinfo)
                start = start.astimezone(timezone.utc)
                end = end.astimezone(timezone.utc)
                current = observed.astimezone(timezone.utc)
            except (TypeError, ValueError):
                continue
            event_type = str(event.get("event_type") or "").lower()
            metadata = dict(event.get("metadata") or {})
            name = str(event.get("summary") or event.get("name") or "").lower()
            is_protected = bool(
                metadata.get("protected_break")
                or event_type in {"rest", "nap"}
                or "protected break" in name
                or "保护性休息" in name
            )
            if start <= current < end and is_protected:
                active_recovery = max(active_recovery, 0.65)
            if event_type == "sleep" and start <= current < end:
                active_recovery = 1.0
            if event_type not in {"rest", "nap", "sleep", "meal"}:
                demanding.append((start, end))
        if active_recovery > 0.0:
            return active_recovery
        current = observed.astimezone(timezone.utc)
        previous = [end for _, end in demanding if end <= current]
        following = [start for start, _ in demanding if start > current]
        if not previous or not following:
            return 0.0
        gap_minutes = (min(following) - max(previous)).total_seconds() / 60.0
        return 0.35 * min(1.0, max(0.0, gap_minutes) / 60.0) if gap_minutes >= 10 else 0.0

    @staticmethod
    def _run_view(row: ModelEvaluationRun) -> dict[str, Any]:
        return {
            "id": str(row.id),
            "dataset_snapshot_id": str(row.dataset_snapshot_id),
            "model_version": row.model_version,
            "evaluation_mode": row.evaluation_mode,
            "evaluation_code_version": row.evaluation_code_version,
            "participant_id": str(row.participant_id)
            if row.participant_id
            else None,
            "metrics": dict(row.metrics_json),
            "created_at": _iso(row.created_at),
            "status": row.status,
        }

    def list_runs(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.database.session() as session:
            rows = session.execute(
                select(ModelEvaluationRun)
                .order_by(desc(ModelEvaluationRun.created_at))
                .limit(limit)
            ).scalars().all()
            return [self._run_view(row) for row in rows]

    def model_comparison_dashboard(
        self, participant_id: uuid.UUID | None = None
    ) -> dict[str, Any]:
        conditions = [
            ModelEvaluationRun.evaluation_mode == "offline_replay",
            ModelEvaluationRun.status == "completed",
            ModelEvaluationRun.evaluation_code_version
            == EVALUATION_CODE_VERSION,
        ]
        if participant_id is not None:
            conditions.append(
                (ModelEvaluationRun.participant_id == participant_id)
                | (ModelEvaluationRun.participant_id.is_(None))
            )
        with self.database.session() as session:
            runs = session.execute(
                select(ModelEvaluationRun)
                .where(*conditions)
                .order_by(desc(ModelEvaluationRun.created_at))
                .limit(50)
            ).scalars().all()
        participant_runs = [
            row for row in runs if row.participant_id == participant_id
        ] if participant_id is not None else []
        cohort_runs = [row for row in runs if row.participant_id is None]
        selected_runs = participant_runs if participant_id is not None else cohort_runs
        latest = self._run_view(selected_runs[0]) if selected_runs else None
        if latest is not None:
            latest["scope"] = "participant" if participant_id is not None else "cohort"
        cohort_latest = self._run_view(cohort_runs[0]) if cohort_runs else None
        if cohort_latest is not None:
            cohort_latest["scope"] = "cohort"
        payload = dict((latest or {}).get("metrics") or {})
        comparison = dict(payload.get("comparison") or {})
        promotion = dict(payload.get("promotion") or {})
        rows = []
        for family in MODEL_FAMILIES:
            metrics = dict(comparison.get(family) or {})
            if not metrics:
                continue
            gate = dict(promotion.get(family) or {})
            rows.append(
                {
                    "model_family": family,
                    "mae": metrics.get("mae"),
                    "rmse": metrics.get("rmse"),
                    "coverage": metrics.get("interval_90_coverage"),
                    "peak_error": metrics.get("peak_magnitude_error"),
                    "peak_timing_error_minutes": metrics.get("peak_timing_error_minutes"),
                    "sample_count": metrics.get("sample_count"),
                    "high_stress_recall": metrics.get("high_stress_recall"),
                    "identifiability": gate.get(
                        "parameter_identifiability"
                    ),
                    "boundary": gate.get("parameter_boundary"),
                    "promotion_warnings": list(gate.get("warnings") or []),
                    "promotion": gate or None,
                }
            )
        current_model = "current_m0"
        learned = (
            self.learned_profiles.runtime_active(participant_id)
            if participant_id is not None
            else None
        )
        if learned is not None:
            parameters = dict(learned.get("parameters") or {})
            selection = dict(parameters.get("model_selection") or {})
            current_model = str(
                selection.get("active_variant")
                or parameters.get("model_family")
                or current_model
            )
        passed = [
            row for row in rows if (row.get("promotion") or {}).get("passed")
        ]
        candidate = min(
            passed or [row for row in rows if row["model_family"] != "current_m0"],
            key=lambda row: float(row["mae"] if row["mae"] is not None else math.inf),
            default=None,
        )
        return {
            "current_model": current_model,
            "candidate_model": (
                candidate["model_family"] if candidate is not None else None
            ),
            "validation_result": (
                candidate.get("promotion") if candidate is not None else None
            ),
            "rows": rows,
            "latest_run": latest,
            "cohort_latest_run": cohort_latest if participant_id is not None else None,
            "run_count": len(selected_runs),
            "cohort_run_count": len(cohort_runs) if participant_id is not None else len(selected_runs),
        }

    def cohort_dashboard(
        self, date_start: date, date_end: date
    ) -> dict[str, Any]:
        lower, upper = self._bounds(date_start, date_end)
        with self.database.session() as session:
            participants = session.execute(
                select(Participant).where(Participant.status == "active")
            ).scalars().all()
            ids = [row.id for row in participants]
            observations = session.execute(
                select(StateObservation.participant_id, StateObservation.observed_at)
                .where(
                    StateObservation.participant_id.in_(ids),
                    StateObservation.observed_at >= lower,
                    StateObservation.observed_at < upper,
                    StateObservation.observation_type.in_(
                        MOMENTARY_OBSERVATION_TYPES
                    ),
                )
            ).all()
            review_pairs = session.execute(
                select(
                    DailyReviewResponse.participant_id,
                    DailyReviewResponse.local_date,
                ).where(
                    DailyReviewResponse.participant_id.in_(ids),
                    DailyReviewResponse.local_date >= date_start,
                    DailyReviewResponse.local_date <= date_end,
                )
            ).all()
            calendar_pairs = session.execute(
                select(
                    CalendarSnapshot.participant_id,
                    CalendarSnapshot.local_date,
                ).where(
                    CalendarSnapshot.participant_id.in_(ids),
                    CalendarSnapshot.local_date >= date_start,
                    CalendarSnapshot.local_date <= date_end,
                )
            ).all()
            semantics = session.execute(
                select(EventSemanticCache.status).where(
                    EventSemanticCache.participant_id.in_(ids),
                    EventSemanticCache.created_at >= lower,
                    EventSemanticCache.created_at < upper,
                )
            ).scalars().all()
            warning_statuses = session.execute(
                select(WarningSchedule.status).where(
                    WarningSchedule.participant_id.in_(ids),
                    WarningSchedule.local_date >= date_start,
                    WarningSchedule.local_date <= date_end,
                )
            ).scalars().all()
            feedback = session.execute(
                select(
                    CareInterventionFeedback.helpfulness,
                    CareInterventionFeedback.action_selected,
                ).where(
                    CareInterventionFeedback.participant_id.in_(ids),
                    CareInterventionFeedback.submitted_at >= lower,
                    CareInterventionFeedback.submitted_at < upper,
                )
            ).all()
            care_events = session.execute(
                select(
                    CareInterventionEvent.status,
                    CareInterventionEvent.delivery_status,
                    CareInterventionEvent.user_action,
                ).where(
                    CareInterventionEvent.participant_id.in_(ids),
                    CareInterventionEvent.scheduled_at >= lower,
                    CareInterventionEvent.scheduled_at < upper,
                )
            ).all()
        eligible = self.eligible_participant_days(participants, date_start, date_end)
        denominator = len(eligible)
        observed_days = {
            (pid, _aware(at).astimezone(self.timezone).date())
            for pid, at in observations
        } & eligible
        review_days = {
            (participant_id, local_date)
            for participant_id, local_date in review_pairs
        } & eligible
        calendar_days = {
            (participant_id, local_date)
            for participant_id, local_date in calendar_pairs
        } & eligible
        matches = [
            item
            for item in self.matches(date_start, date_end)
            if (
                uuid.UUID(item["participant_id"]),
                date.fromisoformat(item["local_date"]),
            )
            in eligible
        ]
        sent = sum(value in {"sent", "escalated"} for value in warning_statuses)
        sent_care = [
            row for row in care_events if row[0] == "sent" or row[1] == "sent"
        ]
        return {
            "date_start": date_start.isoformat(),
            "date_end": date_end.isoformat(),
            "data_completeness": {
                "active_participants": len(participants),
                "eligible_participant_days": denominator,
                "observed_days": len(observed_days),
                "ema_count": len(observations),
                "ema_observed_day_rate": round(
                    len(observed_days) / denominator, 4
                )
                if denominator
                else None,
                "daily_review_completion": round(
                    len(review_days) / denominator, 4
                )
                if denominator
                else None,
                "calendar_coverage": round(
                    len(calendar_days) / denominator, 4
                )
                if denominator
                else None,
                "semantic_complete_rate": round(
                    sum(value == "complete" for value in semantics)
                    / len(semantics),
                    4,
                )
                if semantics
                else None,
            },
            "forecast": self.metrics(matches),
            "warning_care": {
                "sent_count": sent,
                "warnings_per_user_day": round(sent / denominator, 4)
                if denominator
                else None,
                "suppressed_count": sum(
                    value == "suppressed" for value in warning_statuses
                ),
                "helpful_feedback_rate": round(
                    sum(value == "helpful" for value, _ in feedback)
                    / len(feedback),
                    4,
                )
                if feedback
                else None,
                "ignored_rate": round(
                    sum(row[2] is None for row in sent_care) / len(sent_care), 4
                )
                if sent_care
                else None,
            },
        }

    def participant_longitudinal(
        self, participant_id: uuid.UUID, through: date, days: int = 14
    ) -> dict[str, Any]:
        start = through - timedelta(days=days - 1)
        lower, upper = self._bounds(start, through)
        with self.database.session() as session:
            participant = session.get(Participant, participant_id)
            if participant is None:
                raise ValueError("participant not found")
            observations = session.execute(
                select(StateObservation)
                .where(
                    StateObservation.participant_id == participant_id,
                    StateObservation.observed_at >= lower,
                    StateObservation.observed_at < upper,
                    StateObservation.observation_type.in_(
                        MOMENTARY_OBSERVATION_TYPES
                    ),
                )
                .order_by(StateObservation.observed_at)
            ).scalars().all()
            reviews = session.execute(
                select(DailyReviewResponse.local_date).where(
                    DailyReviewResponse.participant_id == participant_id,
                    DailyReviewResponse.local_date >= start,
                    DailyReviewResponse.local_date <= through,
                )
            ).scalars().all()
            warnings = session.execute(
                select(WarningSchedule.id).where(
                    WarningSchedule.participant_id == participant_id,
                    WarningSchedule.local_date >= start,
                    WarningSchedule.local_date <= through,
                )
            ).scalars().all()
            feedback = session.execute(
                select(
                    CareInterventionFeedback.helpfulness,
                    CareInterventionFeedback.action_selected,
                ).where(
                    CareInterventionFeedback.participant_id == participant_id,
                    CareInterventionFeedback.submitted_at >= lower,
                    CareInterventionFeedback.submitted_at < upper,
                )
            ).all()
            calibrations = session.execute(
                select(LearnedModelProfile)
                .where(LearnedModelProfile.participant_id == participant_id)
                .order_by(desc(LearnedModelProfile.version))
            ).scalars().all()
            slow = session.execute(
                select(ParticipantSlowState)
                .where(
                    ParticipantSlowState.participant_id == participant_id,
                    ParticipantSlowState.effective_at >= lower,
                    ParticipantSlowState.effective_at < upper,
                )
                .order_by(ParticipantSlowState.effective_at)
            ).scalars().all()
        daily: dict[date, list[float]] = defaultdict(list)
        for row in observations:
            value = _score(dict(row.payload_json or {}), "stress_0_10")
            if value is not None:
                daily[
                    _aware(row.observed_at).astimezone(self.timezone).date()
                ].append(value)
        stress = [
            {
                "date": day.isoformat(),
                "mean_stress": round(mean(values), 3),
                "ema_count": len(values),
            }
            for day, values in sorted(daily.items())
        ]
        workload = [
            {
                "effective_at": _iso(row.effective_at),
                "rolling_7d_workload": row.rolling_7d_workload,
            }
            for row in slow
            if row.rolling_7d_workload is not None
        ]
        eligible_14 = self.eligible_participant_days([participant], start, through)
        start_7 = through - timedelta(days=6)
        eligible_7 = {pair for pair in eligible_14 if pair[1] >= start_7}
        observed_14 = {(participant_id, day) for day in daily} & eligible_14
        observed_7 = {pair for pair in observed_14 if pair[1] >= start_7}
        review_days = {(participant_id, day) for day in reviews} & eligible_14
        aliases = {
            "S_star": ("S_star", "S_star_init"),
            "reactivity": ("reactivity", "stress_reactivity"),
            "recovery": ("recovery", "recovery_rate"),
            "workload_gain": ("workload_gain",),
        }
        history = []
        for row in calibrations:
            parameters = dict(row.parameters_json or {})
            uncertainty = dict(row.uncertainty_json or {})
            history.append(
                {
                    "version": row.version,
                    "effective_from": row.window_end.isoformat(),
                    "model_version": row.model_version,
                    "validation_result": row.validation_status,
                    "sample_count": row.sample_count,
                    "parameters": {
                        name: {
                            "estimate": next(
                                (parameters[key] for key in keys if key in parameters),
                                None,
                            ),
                            "uncertainty": next(
                                (
                                    uncertainty[key]
                                    for key in keys
                                    if key in uncertainty
                                ),
                                None,
                            ),
                        }
                        for name, keys in aliases.items()
                    },
                }
            )
        calibration = calibrations[0] if calibrations else None
        return {
            "through": through.isoformat(),
            "eligible_day_count_7d": len(eligible_7),
            "eligible_day_count_14d": len(eligible_14),
            "stress_trend_14d": stress,
            "stress_trend_7d": [
                item for item in stress if date.fromisoformat(item["date"]) >= start_7
            ],
            "workload_trend_7d": workload[-7:],
            "ema_observed_day_rate_7d": round(
                len(observed_7) / len(eligible_7), 4
            )
            if eligible_7
            else None,
            "ema_observed_day_rate_14d": round(
                len(observed_14) / len(eligible_14), 4
            )
            if eligible_14
            else None,
            "daily_review_adherence": round(
                len(review_days) / len(eligible_14), 4
            )
            if eligible_14
            else None,
            "warning_count": len(warnings),
            "care_feedback": {
                "count": len(feedback),
                "helpful": sum(value == "helpful" for value, _ in feedback),
            },
            "calibration_version": calibration.version if calibration else None,
            "calibration_status": calibration.validation_status
            if calibration
            else None,
            "parameter_history": history,
        }

    def data_quality(
        self,
        date_start: date,
        date_end: date,
        participant_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        lower, upper = self._bounds(date_start, date_end)
        with self.database.session() as session:
            participants = session.execute(
                select(Participant).where(
                    *(
                        [Participant.id == participant_id]
                        if participant_id
                        else [Participant.status == "active"]
                    )
                )
            ).scalars().all()
            ids = [row.id for row in participants]
            observations = session.execute(
                select(StateObservation).where(
                    StateObservation.participant_id.in_(ids),
                    StateObservation.observed_at >= lower,
                    StateObservation.observed_at < upper,
                    StateObservation.observation_type.in_(
                        MOMENTARY_OBSERVATION_TYPES
                    ),
                )
            ).scalars().all()
            reviews = session.execute(
                select(
                    DailyReviewResponse.participant_id,
                    DailyReviewResponse.local_date,
                ).where(
                    DailyReviewResponse.participant_id.in_(ids),
                    DailyReviewResponse.local_date >= date_start,
                    DailyReviewResponse.local_date <= date_end,
                )
            ).all()
            calendars = session.execute(
                select(CalendarSnapshot).where(
                    CalendarSnapshot.participant_id.in_(ids),
                    CalendarSnapshot.local_date >= date_start,
                    CalendarSnapshot.local_date <= date_end,
                )
            ).scalars().all()
            semantics = session.execute(
                select(EventSemanticCache).where(
                    EventSemanticCache.participant_id.in_(ids),
                    EventSemanticCache.created_at >= lower,
                    EventSemanticCache.created_at < upper,
                )
            ).scalars().all()
        issues: list[dict[str, Any]] = []
        codes = {row.id: row.participant_code for row in participants}
        by_id = {row.id: row for row in participants}
        eligible = self.eligible_participant_days(participants, date_start, date_end)
        review_days = {
            (participant_id, local_date)
            for participant_id, local_date in reviews
        }
        calendar_days = {(row.participant_id, row.local_date) for row in calendars}
        # User-initiated check-ins have no expected assignment, so absence is
        # never labeled missing_ema.
        for pid, day in sorted(eligible, key=lambda item: (str(item[0]), item[1])):
            if (pid, day) not in review_days:
                issues.append(self._issue("daily_review_missing", by_id[pid], day))
            if (pid, day) not in calendar_days:
                issues.append(self._issue("calendar_missing", by_id[pid], day))
        for row in observations:
            delay = (_aware(row.created_at) - _aware(row.observed_at)).total_seconds()
            day = _aware(row.observed_at).astimezone(self.timezone).date()
            if delay > 1800:
                issues.append(
                    self._issue(
                        "late_ema",
                        row,
                        day,
                        {"delay_minutes": round(delay / 60, 1)},
                        codes,
                    )
                )
            if delay > 21600:
                issues.append(
                    self._issue(
                        "backfilled_observation",
                        row,
                        day,
                        {"delay_hours": round(delay / 3600, 1)},
                        codes,
                    )
                )
            if delay < -300:
                issues.append(
                    self._issue(
                        "time_anomaly",
                        row,
                        day,
                        {
                            "knowledge_before_observation_minutes": round(
                                -delay / 60, 1
                            )
                        },
                        codes,
                    )
                )
        for row in calendars:
            if row.degraded or row.snapshot_state != "current":
                issues.append(
                    self._issue(
                        "calendar_degraded",
                        row,
                        row.local_date,
                        {"state": row.snapshot_state},
                        codes,
                    )
                )
            event_ids = [
                str(item.get("id")) for item in row.events_json if item.get("id")
            ]
            if len(event_ids) != len(set(event_ids)):
                issues.append(
                    self._issue("duplicate_event", row, row.local_date, {}, codes)
                )
        for row in semantics:
            if row.status in {"partial", "rejected"}:
                issues.append(
                    self._issue(
                        f"semantic_{row.status}",
                        row,
                        _aware(row.created_at).astimezone(self.timezone).date(),
                        {},
                        codes,
                    )
                )
        for participant in participants:
            if participant.participant_code.casefold().startswith(
                ("test-", "test_", "pytest", "synthetic", "fixture")
            ):
                issues.append(self._issue("synthetic_row", participant, date_start))
        kinds = {item["issue_type"] for item in issues}
        return {
            "date_start": date_start.isoformat(),
            "date_end": date_end.isoformat(),
            "items": issues[:5000],
            "counts": dict(
                sorted(
                    {
                        kind: sum(item["issue_type"] == kind for item in issues)
                        for kind in kinds
                    }.items()
                )
            ),
        }

    @staticmethod
    def _issue(
        kind: str,
        row: Any,
        local_date: date,
        details: dict[str, Any] | None = None,
        codes: dict[uuid.UUID, str] | None = None,
    ) -> dict[str, Any]:
        participant_id = getattr(row, "participant_id", None) or getattr(
            row, "id", None
        )
        return {
            "issue_type": kind,
            "participant_id": str(participant_id),
            "participant_code": getattr(row, "participant_code", None)
            or (codes or {}).get(participant_id),
            "local_date": local_date.isoformat(),
            "details": details or {},
        }
