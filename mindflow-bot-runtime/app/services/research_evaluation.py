"""Stage-2 research matching, evaluation, quality, and snapshot services."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
import math
from statistics import mean, median
from typing import Any
import uuid
from zoneinfo import ZoneInfo

from sqlalchemy import desc, func, select

from app.db import Database
from app.models import (
    CalendarSnapshot,
    CareInterventionEvent,
    CareInterventionFeedback,
    DailyReviewResponse,
    DatasetSnapshot,
    EventSemanticCache,
    ForecastObservationMatch,
    ForecastSnapshot,
    LearnedModelProfile,
    ModelEvaluationRun,
    Participant,
    ParticipantSlowState,
    StateObservation,
    WarningSchedule,
)
from app.repositories import ForecastSnapshotRepository


DATASET_SCHEMA_VERSION = "mindflow-research-dataset-v1"
MATCH_TOLERANCE_SECONDS = 150


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, (date, datetime)) else None


def _score(payload: dict[str, Any], key: str) -> float | None:
    try:
        value = float(payload[key])
    except (KeyError, TypeError, ValueError):
        return None
    return value if 0 <= value <= 10 else None


class ResearchEvaluationService:
    def __init__(self, database: Database, timezone_name: str):
        self.database = database
        self.timezone = ZoneInfo(timezone_name)
        self.forecasts = ForecastSnapshotRepository(database)

    def _bounds(self, start: date, end: date) -> tuple[datetime, datetime]:
        if start > end or (end - start).days > 365:
            raise ValueError("invalid date range")
        lower = datetime.combine(start, time.min, self.timezone).astimezone(timezone.utc)
        upper = datetime.combine(end + timedelta(days=1), time.min, self.timezone).astimezone(timezone.utc)
        return lower, upper

    @staticmethod
    def _point_time(local_date: date, raw: Any, timezone_value: ZoneInfo) -> datetime | None:
        text = str(raw or "")
        try:
            hour, minute = (int(part) for part in text[:5].split(":"))
        except (TypeError, ValueError):
            return None
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        return datetime.combine(local_date, time(hour, minute), timezone_value).astimezone(timezone.utc)

    def _nearest_point(
        self, forecast: dict[str, Any], observed_at: datetime
    ) -> tuple[dict[str, Any], datetime] | None:
        candidates = []
        forecast_date = forecast["local_date"]
        if isinstance(forecast_date, str):
            forecast_date = date.fromisoformat(forecast_date)
        for point in forecast.get("curve") or []:
            timestamp = self._point_time(forecast_date, point.get("time"), self.timezone)
            if timestamp is not None:
                candidates.append((abs((timestamp - observed_at).total_seconds()), point, timestamp))
        if not candidates:
            return None
        distance, point, timestamp = min(candidates, key=lambda item: item[0])
        return (point, timestamp) if distance <= MATCH_TOLERANCE_SECONDS else None

    @staticmethod
    def _event_context(forecast: dict[str, Any], timestamp: datetime) -> dict[str, Any]:
        event_types: set[str] = set()
        courses: set[str] = set()
        events = (forecast.get("output") or {}).get("classified_calendar_events") or []
        for event in events:
            try:
                start = datetime.fromisoformat(str(event.get("start_time")).replace("Z", "+00:00"))
                end = datetime.fromisoformat(str(event.get("end_time")).replace("Z", "+00:00"))
                start, end = _aware(start), _aware(end)
            except (TypeError, ValueError):
                continue
            if not start <= timestamp < end:
                continue
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
        ]
        if participant_id is not None:
            conditions.append(StateObservation.participant_id == participant_id)
        with self.database.session() as session:
            observations = session.execute(
                select(StateObservation).where(*conditions).order_by(StateObservation.observed_at)
            ).scalars().all()
        created = updated = unmatched = 0
        for observation in observations:
            payload = dict(observation.payload_json or {})
            actual = _score(payload, "stress_0_10")
            if actual is None:
                unmatched += 1
                continue
            observed_at, created_at = _aware(observation.observed_at), _aware(observation.created_at)
            local_date = observed_at.astimezone(self.timezone).date()
            causal_cutoff = min(observed_at, created_at, cutoff)
            forecast = self.forecasts.current_at(observation.participant_id, local_date, causal_cutoff)
            if forecast is None:
                unmatched += 1
                continue
            nearest = self._nearest_point(forecast, observed_at)
            if nearest is None:
                unmatched += 1
                continue
            point, forecast_timestamp = nearest
            predicted = _score(point, "stress_0_10")
            if predicted is None:
                unmatched += 1
                continue
            interval = point.get("stress_interval_90_0_10") or {}
            lower_value = _score(interval, "lower")
            upper_value = _score(interval, "upper")
            peak = max(
                (forecast.get("curve") or []),
                key=lambda item: float(item.get("stress_0_10") or -1),
                default={},
            )
            context = {
                **self._event_context(forecast, forecast_timestamp),
                "time_of_day": observed_at.astimezone(self.timezone).strftime("%H:%M"),
                "forecast_point_time": forecast_timestamp.astimezone(self.timezone).strftime("%H:%M"),
                "weekday": observed_at.astimezone(self.timezone).strftime("%A"),
                "workload_0_10": _score(payload, "current_workload_0_10"),
                "algorithm_version": forecast.get("algorithm_version"),
                "forecast_peak_stress": _score(peak, "stress_0_10"),
                "forecast_peak_time": peak.get("time"),
            }
            forecast_id = uuid.UUID(str(forecast["id"]))
            with self.database.session() as session:
                row = session.execute(
                    select(ForecastObservationMatch).where(
                        ForecastObservationMatch.observation_id == observation.id,
                        ForecastObservationMatch.forecast_id == forecast_id,
                    )
                ).scalar_one_or_none()
                values = dict(
                    participant_id=observation.participant_id,
                    local_date=local_date,
                    forecast_id=forecast_id,
                    forecast_version=forecast["forecast_version"],
                    forecast_timestamp=forecast_timestamp,
                    observation_id=observation.id,
                    observed_at=observed_at,
                    predicted_stress=predicted,
                    actual_stress=actual,
                    residual=actual - predicted,
                    prediction_lower=lower_value,
                    prediction_upper=upper_value,
                    context_json=context,
                )
                if row is None:
                    session.add(ForecastObservationMatch(**values))
                    created += 1
                else:
                    for key, value in values.items():
                        setattr(row, key, value)
                    updated += 1
        return {"created": created, "updated": updated, "unmatched": unmatched, "examined": len(observations)}

    @staticmethod
    def _match_view(row: ForecastObservationMatch) -> dict[str, Any]:
        return {
            "id": str(row.id), "participant_id": str(row.participant_id),
            "local_date": row.local_date.isoformat(), "forecast_version": row.forecast_version,
            "forecast_timestamp": _iso(row.forecast_timestamp), "observation_id": str(row.observation_id),
            "observed_at": _iso(row.observed_at), "predicted_stress": row.predicted_stress,
            "actual_stress": row.actual_stress, "residual": row.residual,
            "prediction_lower": row.prediction_lower, "prediction_upper": row.prediction_upper,
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
        conditions = [ForecastObservationMatch.local_date >= date_start, ForecastObservationMatch.local_date <= date_end]
        if participant_id is not None:
            conditions.append(ForecastObservationMatch.participant_id == participant_id)
        statement = select(ForecastObservationMatch)
        if observation_cutoff is not None:
            statement = statement.join(
                StateObservation,
                StateObservation.id == ForecastObservationMatch.observation_id,
            )
            conditions.append(
                StateObservation.created_at <= _aware(observation_cutoff)
            )
        with self.database.session() as session:
            rows = session.execute(statement.where(*conditions).order_by(ForecastObservationMatch.observed_at)).scalars().all()
            return [self._match_view(row) for row in rows]

    @staticmethod
    def metrics(matches: list[dict[str, Any]]) -> dict[str, Any]:
        if not matches:
            return {"sample_count": 0, "mae": None, "rmse": None, "median_absolute_error": None,
                    "interval_nominal_coverage": 0.9, "interval_90_coverage": None, "mean_interval_width": None,
                    "peak_magnitude_error": None, "peak_timing_error_minutes": None}
        residuals = [float(item["residual"]) for item in matches]
        absolute = [abs(value) for value in residuals]
        intervals = [item for item in matches if item["prediction_lower"] is not None and item["prediction_upper"] is not None]
        coverage = [item["prediction_lower"] <= item["actual_stress"] <= item["prediction_upper"] for item in intervals]
        widths = [item["prediction_upper"] - item["prediction_lower"] for item in intervals]
        peak_magnitude, peak_timing = [], []
        groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for item in matches:
            groups[(item["local_date"], item["forecast_version"])].append(item)
        for items in groups.values():
            actual_peak = max(items, key=lambda value: value["actual_stress"])
            context = actual_peak["context"]
            predicted_peak = context.get("forecast_peak_stress")
            predicted_time = context.get("forecast_peak_time")
            if predicted_peak is not None:
                peak_magnitude.append(abs(actual_peak["actual_stress"] - float(predicted_peak)))
            if predicted_time:
                try:
                    ah, am = (
                        int(part)
                        for part in str(context.get("time_of_day"))[:5].split(":")
                    )
                    ph, pm = (int(part) for part in str(predicted_time)[:5].split(":"))
                    peak_timing.append(abs((ah * 60 + am) - (ph * 60 + pm)))
                except (TypeError, ValueError):
                    pass
        return {
            "sample_count": len(matches), "mae": round(mean(absolute), 4),
            "rmse": round(math.sqrt(mean([value * value for value in residuals])), 4),
            "median_absolute_error": round(median(absolute), 4),
            "interval_nominal_coverage": 0.9,
            "interval_90_coverage": round(mean(coverage), 4) if coverage else None,
            "mean_interval_width": round(mean(widths), 4) if widths else None,
            "peak_magnitude_error": round(mean(peak_magnitude), 4) if peak_magnitude else None,
            "peak_timing_error_minutes": round(mean(peak_timing), 2) if peak_timing else None,
        }

    @staticmethod
    def residual_diagnostics(matches: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        dimensions: dict[str, dict[str, list[float]]] = {
            key: defaultdict(list) for key in ("time_of_day", "workload", "event_type", "course", "weekday")
        }
        for item in matches:
            residual, context = float(item["residual"]), item["context"]
            hour = int(str(context.get("time_of_day") or "00")[:2])
            bucket = "00:00–06:00" if hour < 6 else "06:00–10:00" if hour < 10 else "10:00–14:00" if hour < 14 else "14:00–18:00" if hour < 18 else "18:00–24:00"
            dimensions["time_of_day"][bucket].append(residual)
            workload = context.get("workload_0_10")
            if workload is not None:
                workload_start = min(int(float(workload)), 9) // 2 * 2
                dimensions["workload"][f"{workload_start}–{workload_start + 2}"].append(residual)
            for value in context.get("event_types") or ["none"]:
                dimensions["event_type"][str(value)].append(residual)
            for value in context.get("courses") or ["none"]:
                dimensions["course"][str(value)].append(residual)
            dimensions["weekday"][str(context.get("weekday") or "unknown")].append(residual)
        result = {}
        for dimension, groups in dimensions.items():
            result[dimension] = [
                {"group": group, "mean_residual": round(mean(values), 4),
                 "median_residual": round(median(values), 4),
                 "mae": round(mean(abs(value) for value in values), 4), "sample_count": len(values)}
                for group, values in sorted(groups.items())
            ]
        return result

    def evaluation(self, date_start: date, date_end: date, participant_id: uuid.UUID | None = None) -> dict[str, Any]:
        matches = self.matches(date_start, date_end, participant_id)
        return {"date_start": date_start.isoformat(), "date_end": date_end.isoformat(),
                "metrics": self.metrics(matches), "residual_diagnostics": self.residual_diagnostics(matches),
                "matches": matches}

    def create_dataset_snapshot(self, *, date_start: date, date_end: date, participant_filter: dict[str, Any] | None = None,
                                observation_cutoff: datetime | None = None, calendar_cutoff: datetime | None = None) -> dict[str, Any]:
        lower, upper = self._bounds(date_start, date_end)
        participant_filter = dict(participant_filter or {})
        allowed = {"participant_codes"}
        if set(participant_filter) - allowed:
            raise ValueError("unsupported participant filter")
        codes = participant_filter.get("participant_codes") or []
        if not isinstance(codes, (list, tuple)):
            raise ValueError("participant_codes must be an array")
        participant_filter["participant_codes"] = sorted(
            {str(value).strip() for value in codes if str(value).strip()}
        )
        observation_cutoff = _aware(observation_cutoff or datetime.now(timezone.utc))
        calendar_cutoff = _aware(calendar_cutoff or observation_cutoff)
        with self.database.session() as session:
            participant_query = select(Participant.id)
            codes = participant_filter["participant_codes"]
            if codes:
                participant_query = participant_query.where(Participant.participant_code.in_([str(v) for v in codes]))
            participant_ids = list(session.execute(participant_query).scalars().all())
            manifest = {
                "participant_count": len(participant_ids),
                "observation_count": session.scalar(select(func.count()).select_from(StateObservation).where(
                    StateObservation.participant_id.in_(participant_ids), StateObservation.observed_at >= lower,
                    StateObservation.observed_at < upper, StateObservation.created_at <= observation_cutoff)) or 0,
                "calendar_snapshot_count": session.scalar(select(func.count()).select_from(CalendarSnapshot).where(
                    CalendarSnapshot.participant_id.in_(participant_ids), CalendarSnapshot.local_date >= date_start,
                    CalendarSnapshot.local_date <= date_end, CalendarSnapshot.updated_at <= calendar_cutoff)) or 0,
                "forecast_count": session.scalar(select(func.count()).select_from(ForecastSnapshot).where(
                    ForecastSnapshot.participant_id.in_(participant_ids), ForecastSnapshot.local_date >= date_start,
                    ForecastSnapshot.local_date <= date_end, ForecastSnapshot.generated_at <= observation_cutoff)) or 0,
                "participant_ids": [str(value) for value in participant_ids],
            }
            row = DatasetSnapshot(date_start=date_start, date_end=date_end, participant_filter=participant_filter,
                                  observation_cutoff=observation_cutoff, calendar_cutoff=calendar_cutoff,
                                  schema_version=DATASET_SCHEMA_VERSION, manifest_json=manifest)
            session.add(row); session.flush()
            return self._snapshot_view(row)

    @staticmethod
    def _snapshot_view(row: DatasetSnapshot) -> dict[str, Any]:
        return {"id": str(row.id), "created_at": _iso(row.created_at), "date_start": row.date_start.isoformat(),
                "date_end": row.date_end.isoformat(), "participant_filter": dict(row.participant_filter),
                "observation_cutoff": _iso(row.observation_cutoff), "calendar_cutoff": _iso(row.calendar_cutoff),
                "schema_version": row.schema_version, "manifest": dict(row.manifest_json)}

    def list_snapshots(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.database.session() as session:
            rows = session.execute(select(DatasetSnapshot).order_by(desc(DatasetSnapshot.created_at)).limit(limit)).scalars().all()
            return [self._snapshot_view(row) for row in rows]

    def create_evaluation_run(self, snapshot_id: uuid.UUID, model_version: str, participant_id: uuid.UUID | None = None) -> dict[str, Any]:
        model_version = str(model_version).strip()
        if not model_version or len(model_version) > 64:
            raise ValueError("model_version must contain 1 to 64 characters")
        with self.database.session() as session:
            snapshot = session.get(DatasetSnapshot, snapshot_id)
            if snapshot is None:
                raise ValueError("dataset snapshot not found")
            start, end, cutoff = snapshot.date_start, snapshot.date_end, _aware(snapshot.observation_cutoff)
            allowed_ids = {
                uuid.UUID(str(value))
                for value in (snapshot.manifest_json or {}).get("participant_ids", [])
            }
        if participant_id is not None and participant_id not in allowed_ids:
            raise ValueError("participant is outside dataset snapshot")
        target_ids = [participant_id] if participant_id is not None else sorted(allowed_ids, key=str)
        for target_id in target_ids:
            self.rebuild_matches(
                date_start=start,
                date_end=end,
                participant_id=target_id,
                observation_cutoff=cutoff,
            )
        matches = [
            item for item in self.matches(
                start,
                end,
                participant_id,
                observation_cutoff=cutoff,
            )
            if uuid.UUID(item["participant_id"]) in allowed_ids
            and item["context"].get("algorithm_version") == model_version
        ]
        metrics = {"metrics": self.metrics(matches), "residual_diagnostics": self.residual_diagnostics(matches),
                   "matched_observation_count": len(matches)}
        with self.database.session() as session:
            row = ModelEvaluationRun(dataset_snapshot_id=snapshot_id, model_version=model_version,
                                     participant_id=participant_id, metrics_json=metrics, status="completed")
            session.add(row); session.flush(); return self._run_view(row)

    @staticmethod
    def _run_view(row: ModelEvaluationRun) -> dict[str, Any]:
        return {"id": str(row.id), "dataset_snapshot_id": str(row.dataset_snapshot_id),
                "model_version": row.model_version, "participant_id": str(row.participant_id) if row.participant_id else None,
                "metrics": dict(row.metrics_json), "created_at": _iso(row.created_at), "status": row.status}

    def list_runs(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.database.session() as session:
            rows = session.execute(select(ModelEvaluationRun).order_by(desc(ModelEvaluationRun.created_at)).limit(limit)).scalars().all()
            return [self._run_view(row) for row in rows]

    def cohort_dashboard(self, date_start: date, date_end: date) -> dict[str, Any]:
        lower, upper = self._bounds(date_start, date_end); days = (date_end - date_start).days + 1
        with self.database.session() as session:
            active_ids = list(session.execute(select(Participant.id).where(Participant.status == "active")).scalars().all())
            active = len(active_ids)
            ema_count = session.scalar(select(func.count()).select_from(StateObservation).where(StateObservation.participant_id.in_(active_ids), StateObservation.observed_at >= lower, StateObservation.observed_at < upper)) or 0
            observed_pairs = session.execute(select(StateObservation.participant_id, StateObservation.observed_at).where(StateObservation.participant_id.in_(active_ids), StateObservation.observed_at >= lower, StateObservation.observed_at < upper)).all()
            observed_days = len({(pid, _aware(at).astimezone(self.timezone).date()) for pid, at in observed_pairs})
            review_pairs = session.execute(select(DailyReviewResponse.participant_id, DailyReviewResponse.local_date).where(DailyReviewResponse.participant_id.in_(active_ids), DailyReviewResponse.local_date >= date_start, DailyReviewResponse.local_date <= date_end)).all()
            review_days = len({(pid, local_date) for pid, local_date in review_pairs})
            calendar_pairs = session.execute(select(CalendarSnapshot.participant_id, CalendarSnapshot.local_date).where(CalendarSnapshot.participant_id.in_(active_ids), CalendarSnapshot.local_date >= date_start, CalendarSnapshot.local_date <= date_end)).all()
            calendar_days = len({(pid, local_date) for pid, local_date in calendar_pairs})
            semantics = session.execute(select(EventSemanticCache.status).where(EventSemanticCache.participant_id.in_(active_ids), EventSemanticCache.created_at >= lower, EventSemanticCache.created_at < upper)).scalars().all()
            warning_statuses = session.execute(select(WarningSchedule.status).where(WarningSchedule.participant_id.in_(active_ids), WarningSchedule.local_date >= date_start, WarningSchedule.local_date <= date_end)).scalars().all()
            feedback = session.execute(select(CareInterventionFeedback.helpfulness, CareInterventionFeedback.action_selected).where(CareInterventionFeedback.participant_id.in_(active_ids), CareInterventionFeedback.submitted_at >= lower, CareInterventionFeedback.submitted_at < upper)).all()
            care_events = session.execute(select(CareInterventionEvent.status, CareInterventionEvent.delivery_status, CareInterventionEvent.user_action).where(CareInterventionEvent.participant_id.in_(active_ids), CareInterventionEvent.scheduled_at >= lower, CareInterventionEvent.scheduled_at < upper)).all()
        denominator = active * days
        active_id_set = set(active_ids)
        cohort_matches = [
            item for item in self.matches(date_start, date_end)
            if uuid.UUID(item["participant_id"]) in active_id_set
        ]
        sent_care = [row for row in care_events if row[0] == "sent" or row[1] == "sent"]
        return {"date_start": date_start.isoformat(), "date_end": date_end.isoformat(),
                "data_completeness": {"active_participants": active, "observed_days": observed_days, "ema_count": ema_count,
                    "ema_adherence": round(observed_days / denominator, 4) if denominator else None,
                    "daily_review_completion": round(review_days / denominator, 4) if denominator else None,
                    "calendar_coverage": round(calendar_days / denominator, 4) if denominator else None,
                    "semantic_complete_rate": round(sum(v == "complete" for v in semantics) / len(semantics), 4) if semantics else None},
                "forecast": self.metrics(cohort_matches),
                "warning_care": {"sent_count": sum(v in {"sent", "escalated"} for v in warning_statuses),
                    "warnings_per_user_day": round(sum(v in {"sent", "escalated"} for v in warning_statuses) / denominator, 4) if denominator else None,
                    "suppressed_count": sum(v == "suppressed" for v in warning_statuses),
                    "helpful_feedback_rate": round(sum(h == "helpful" for h, _ in feedback) / len(feedback), 4) if feedback else None,
                    "ignored_rate": round(sum(row[2] is None for row in sent_care) / len(sent_care), 4) if sent_care else None}}

    def participant_longitudinal(self, participant_id: uuid.UUID, through: date, days: int = 14) -> dict[str, Any]:
        start = through - timedelta(days=days - 1); lower, upper = self._bounds(start, through)
        with self.database.session() as session:
            observations = session.execute(select(StateObservation).where(StateObservation.participant_id == participant_id,
                StateObservation.observed_at >= lower, StateObservation.observed_at < upper).order_by(StateObservation.observed_at)).scalars().all()
            reviews = session.scalar(select(func.count()).select_from(DailyReviewResponse).where(DailyReviewResponse.participant_id == participant_id,
                DailyReviewResponse.local_date >= start, DailyReviewResponse.local_date <= through)) or 0
            warnings = session.scalar(select(func.count()).select_from(WarningSchedule).where(WarningSchedule.participant_id == participant_id,
                WarningSchedule.local_date >= start, WarningSchedule.local_date <= through)) or 0
            feedback = session.execute(select(CareInterventionFeedback.helpfulness, CareInterventionFeedback.action_selected).where(
                CareInterventionFeedback.participant_id == participant_id, CareInterventionFeedback.submitted_at >= lower,
                CareInterventionFeedback.submitted_at < upper)).all()
            calibrations = session.execute(select(LearnedModelProfile).where(LearnedModelProfile.participant_id == participant_id).order_by(desc(LearnedModelProfile.version))).scalars().all()
            slow = session.execute(select(ParticipantSlowState).where(ParticipantSlowState.participant_id == participant_id,
                ParticipantSlowState.effective_at >= lower, ParticipantSlowState.effective_at < upper).order_by(ParticipantSlowState.effective_at)).scalars().all()
        daily: dict[date, list[float]] = defaultdict(list)
        for row in observations:
            value = _score(dict(row.payload_json or {}), "stress_0_10")
            if value is not None: daily[_aware(row.observed_at).astimezone(self.timezone).date()].append(value)
        stress_trend = [{"date": day.isoformat(), "mean_stress": round(mean(values), 3), "ema_count": len(values)} for day, values in sorted(daily.items())]
        workload = [{"effective_at": _iso(row.effective_at), "rolling_7d_workload": row.rolling_7d_workload} for row in slow if row.rolling_7d_workload is not None]
        observed_7d = sum(day >= through - timedelta(days=6) for day in daily)
        parameter_aliases = {
            "S_star": ("S_star", "S_star_init"),
            "reactivity": ("reactivity", "stress_reactivity"),
            "recovery": ("recovery", "recovery_rate"),
            "workload_gain": ("workload_gain",),
        }
        parameter_history = []
        for row in calibrations:
            parameters = dict(row.parameters_json or {})
            uncertainty = dict(row.uncertainty_json or {})
            parameter_history.append({
                "version": row.version,
                "effective_from": row.window_end.isoformat(),
                "model_version": row.model_version,
                "validation_result": row.validation_status,
                "sample_count": row.sample_count,
                "parameters": {
                    name: {
                        "estimate": next(
                            (parameters[key] for key in aliases if key in parameters),
                            None,
                        ),
                        "uncertainty": next(
                            (uncertainty[key] for key in aliases if key in uncertainty),
                            None,
                        ),
                    }
                    for name, aliases in parameter_aliases.items()
                },
            })
        calibration = calibrations[0] if calibrations else None
        return {"through": through.isoformat(), "stress_trend_14d": stress_trend,
                "stress_trend_7d": [item for item in stress_trend if date.fromisoformat(item["date"]) >= through - timedelta(days=6)],
                "workload_trend_7d": workload[-7:], "ema_adherence_7d": round(observed_7d / 7, 4),
                "ema_adherence_14d": round(len(daily) / days, 4), "daily_review_adherence": round(reviews / days, 4),
                "warning_count": warnings, "care_feedback": {"count": len(feedback), "helpful": sum(h == "helpful" for h, _ in feedback)},
                "calibration_version": calibration.version if calibration else None,
                "calibration_status": calibration.validation_status if calibration else None,
                "parameter_history": parameter_history}

    def data_quality(self, date_start: date, date_end: date, participant_id: uuid.UUID | None = None) -> dict[str, Any]:
        lower, upper = self._bounds(date_start, date_end)
        with self.database.session() as session:
            participants = session.execute(select(Participant).where(*([Participant.id == participant_id] if participant_id else [Participant.status == "active"]))).scalars().all()
            pids = [row.id for row in participants]
            observations = session.execute(select(StateObservation).where(StateObservation.participant_id.in_(pids), StateObservation.observed_at >= lower, StateObservation.observed_at < upper)).scalars().all()
            reviews = session.execute(select(DailyReviewResponse.participant_id, DailyReviewResponse.local_date).where(DailyReviewResponse.participant_id.in_(pids), DailyReviewResponse.local_date >= date_start, DailyReviewResponse.local_date <= date_end)).all()
            calendars = session.execute(select(CalendarSnapshot).where(CalendarSnapshot.participant_id.in_(pids), CalendarSnapshot.local_date >= date_start, CalendarSnapshot.local_date <= date_end)).scalars().all()
            semantics = session.execute(select(EventSemanticCache).where(EventSemanticCache.participant_id.in_(pids), EventSemanticCache.created_at >= lower, EventSemanticCache.created_at < upper)).scalars().all()
        issues: list[dict[str, Any]] = []
        codes = {row.id: row.participant_code for row in participants}
        observed_days = {(row.participant_id, _aware(row.observed_at).astimezone(self.timezone).date()) for row in observations}
        review_days = {(pid, local_date) for pid, local_date in reviews}
        calendar_days = {(row.participant_id, row.local_date) for row in calendars}
        for participant in participants:
            for offset in range((date_end - date_start).days + 1):
                day = date_start + timedelta(days=offset)
                if (participant.id, day) not in observed_days: issues.append(self._issue("missing_ema", participant, day))
                if (participant.id, day) not in review_days: issues.append(self._issue("daily_review_missing", participant, day))
                if (participant.id, day) not in calendar_days: issues.append(self._issue("calendar_missing", participant, day))
        for row in observations:
            delay = (_aware(row.created_at) - _aware(row.observed_at)).total_seconds()
            day = _aware(row.observed_at).astimezone(self.timezone).date()
            if delay > 1800: issues.append(self._issue("late_ema", row, day, {"delay_minutes": round(delay / 60, 1)}, codes))
            if delay > 21600: issues.append(self._issue("backfilled_observation", row, day, {"delay_hours": round(delay / 3600, 1)}, codes))
            if delay < -300: issues.append(self._issue("time_anomaly", row, day, {"knowledge_before_observation_minutes": round(-delay / 60, 1)}, codes))
        for row in calendars:
            if row.degraded or row.snapshot_state != "current": issues.append(self._issue("calendar_degraded", row, row.local_date, {"state": row.snapshot_state}, codes))
            event_ids = [str(item.get("id") or "") for item in row.events_json if item.get("id")]
            if len(event_ids) != len(set(event_ids)): issues.append(self._issue("duplicate_event", row, row.local_date, {}, codes))
        for row in semantics:
            if row.status in {"partial", "rejected"}: issues.append(self._issue(f"semantic_{row.status}", row, _aware(row.created_at).astimezone(self.timezone).date(), {}, codes))
        for participant in participants:
            folded = participant.participant_code.casefold()
            if folded.startswith(("test-", "test_", "pytest", "synthetic", "fixture")):
                issues.append(self._issue("synthetic_row", participant, date_start))
        return {"date_start": date_start.isoformat(), "date_end": date_end.isoformat(), "items": issues[:5000],
                "counts": dict(sorted({kind: sum(item["issue_type"] == kind for item in issues) for kind in {item["issue_type"] for item in issues}}.items()))}

    @staticmethod
    def _issue(kind: str, row: Any, local_date: date, details: dict[str, Any] | None = None,
               codes: dict[uuid.UUID, str] | None = None) -> dict[str, Any]:
        pid = getattr(row, "participant_id", None) or getattr(row, "id", None)
        code = getattr(row, "participant_code", None) or (codes or {}).get(pid)
        return {"issue_type": kind, "participant_id": str(pid), "participant_code": code,
                "local_date": local_date.isoformat(), "details": details or {}}
