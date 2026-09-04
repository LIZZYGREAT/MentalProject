"""Evidence-backed participant overview for the Admin console."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
import math
import statistics
from typing import Any
import uuid
from zoneinfo import ZoneInfo

from sqlalchemy import desc, func, select

from app.db import Database
from app.models import (
    BotEvent,
    CareInterventionEvent,
    DailyReviewResponse,
    ForecastSnapshot,
    LearnedModelProfile,
    Participant,
    ParticipantProfile,
    ParticipantSlowState,
    StateObservation,
    WarningSchedule,
)
from app.services.care_effectiveness import CareEffectivenessService
from app.services.curve_analysis import HIGH_RISK, MEDIUM_RISK


OVERVIEW_SCHEMA_VERSION = "participant-overview.v2"


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, dict):
        value = value.get("estimate")
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _score(value: float, lower: float, upper: float) -> float:
    return round(max(0.0, min(100.0, (value - lower) / (upper - lower) * 100)), 1)


class ParticipantOverviewService:
    """Aggregate existing read models without inventing missing measurements."""

    def __init__(self, database: Database, timezone_name: str):
        self.database = database
        self.timezone = ZoneInfo(timezone_name)
        self.care_effects = CareEffectivenessService(database, timezone_name)

    def build(
        self,
        participant_id: uuid.UUID,
        *,
        through: date | None = None,
    ) -> dict[str, Any]:
        through = through or datetime.now(self.timezone).date()
        start_7 = through - timedelta(days=6)
        start_14 = through - timedelta(days=13)
        start_28 = through - timedelta(days=27)
        lower_14 = datetime.combine(start_14, time.min, self.timezone).astimezone(timezone.utc)
        upper = datetime.combine(through + timedelta(days=1), time.min, self.timezone).astimezone(timezone.utc)
        lower_28 = datetime.combine(start_28, time.min, self.timezone).astimezone(timezone.utc)

        with self.database.session() as session:
            participant = session.get(Participant, participant_id)
            if participant is None:
                raise LookupError("participant_not_found")
            profile = session.execute(
                select(ParticipantProfile)
                .where(
                    ParticipantProfile.participant_id == participant_id,
                    ParticipantProfile.created_at < upper,
                )
                .order_by(desc(ParticipantProfile.version))
                .limit(1)
            ).scalar_one_or_none()
            learned = session.execute(
                select(LearnedModelProfile)
                .where(
                    LearnedModelProfile.participant_id == participant_id,
                    LearnedModelProfile.created_at < upper,
                )
                .order_by(desc(LearnedModelProfile.version))
                .limit(1)
            ).scalar_one_or_none()
            slow = session.execute(
                select(ParticipantSlowState)
                .where(
                    ParticipantSlowState.participant_id == participant_id,
                    ParticipantSlowState.effective_at < upper,
                    ParticipantSlowState.created_at < upper,
                )
                .order_by(
                    desc(ParticipantSlowState.effective_at),
                    desc(ParticipantSlowState.created_at),
                )
                .limit(1)
            ).scalar_one_or_none()
            observation = session.execute(
                select(StateObservation)
                .where(
                    StateObservation.participant_id == participant_id,
                    StateObservation.observed_at < upper,
                    StateObservation.created_at < upper,
                )
                .order_by(
                    desc(StateObservation.observed_at),
                    desc(StateObservation.created_at),
                )
                .limit(1)
            ).scalar_one_or_none()
            forecasts = session.execute(
                select(ForecastSnapshot)
                .where(
                    ForecastSnapshot.participant_id == participant_id,
                    ForecastSnapshot.local_date >= start_14,
                    ForecastSnapshot.local_date <= through,
                    ForecastSnapshot.valid.is_(True),
                    ForecastSnapshot.generated_at < upper,
                )
                .order_by(ForecastSnapshot.local_date, ForecastSnapshot.generated_at)
            ).scalars().all()
            forecasts_by_date = {forecast.local_date: forecast for forecast in forecasts}
            window_forecasts = list(forecasts_by_date.values())
            target_forecast = forecasts_by_date.get(through)
            latest_forecast = window_forecasts[-1] if window_forecasts else session.execute(
                select(ForecastSnapshot)
                .where(
                    ForecastSnapshot.participant_id == participant_id,
                    ForecastSnapshot.local_date <= through,
                    ForecastSnapshot.valid.is_(True),
                    ForecastSnapshot.generated_at < upper,
                )
                .order_by(desc(ForecastSnapshot.local_date), desc(ForecastSnapshot.generated_at))
                .limit(1)
            ).scalar_one_or_none()
            observations = session.execute(
                select(StateObservation)
                .where(
                    StateObservation.participant_id == participant_id,
                    StateObservation.observed_at >= lower_14,
                    StateObservation.observed_at < upper,
                    StateObservation.created_at < upper,
                )
            ).scalars().all()
            review_days = session.execute(
                select(DailyReviewResponse.local_date)
                .where(
                    DailyReviewResponse.participant_id == participant_id,
                    DailyReviewResponse.local_date >= start_14,
                    DailyReviewResponse.local_date <= through,
                )
                .distinct()
            ).scalars().all()
            warnings_7d = session.scalar(
                select(func.count()).select_from(WarningSchedule).where(
                    WarningSchedule.participant_id == participant_id,
                    WarningSchedule.local_date >= start_7,
                    WarningSchedule.local_date <= through,
                )
            ) or 0
            care_rows = session.execute(
                select(CareInterventionEvent.receptivity_score)
                .where(
                    CareInterventionEvent.participant_id == participant_id,
                    CareInterventionEvent.scheduled_at >= lower_28,
                    CareInterventionEvent.scheduled_at < upper,
                )
            ).scalars().all()
            last_message_at = session.scalar(
                select(func.max(BotEvent.received_at)).where(
                    BotEvent.participant_id == participant_id,
                    BotEvent.received_at < upper,
                )
            )

        eligible_start = max(start_14, _aware(participant.created_at).astimezone(self.timezone).date())
        eligible_days = max(0, (through - eligible_start).days + 1)
        observed_days = {
            _aware(row.observed_at).astimezone(self.timezone).date()
            for row in observations
        }
        forecast_days = {row.local_date for row in window_forecasts}
        ema_day_rate = min(1.0, len(observed_days) / eligible_days) if eligible_days else None
        forecast_day_rate = min(1.0, len(forecast_days) / eligible_days) if eligible_days else None
        review_rate = len(set(review_days)) / eligible_days if eligible_days else None
        coverage_parts = [value for value in (ema_day_rate, forecast_day_rate) if value is not None]
        if profile is not None:
            coverage_parts.append(1.0)
        if learned is not None:
            coverage_parts.append(min(1.0, learned.sample_count / 30.0))
        evidence_coverage = statistics.fmean(coverage_parts) if coverage_parts else None

        target_curve = list(target_forecast.curve_json or []) if target_forecast else []
        valid_stress = [
            value for value in (_number(point.get("stress_0_10")) for point in target_curve)
            if value is not None and 0 <= value <= 10
        ]
        peak_stress = max(valid_stress) if valid_stress else None
        peak_point = next(
            (
                point for point in target_curve
                if _number(point.get("stress_0_10")) == peak_stress
            ),
            None,
        )
        attention_level = (
            None if peak_stress is None else
            "high" if peak_stress >= HIGH_RISK else
            "medium" if peak_stress >= MEDIUM_RISK else "low"
        )

        dimensions: list[dict[str, Any]] = []

        def dimension(
            key: str,
            label: str,
            value: float | None,
            *,
            source: str,
            source_field: str,
            version: Any,
            confidence: float | None,
            normalization: str,
            sample_count: int | None = None,
            description: str | None = None,
        ) -> None:
            if value is None:
                return
            dimensions.append({
                "key": key,
                "label": label,
                "score_0_100": round(value, 1),
                "source": source,
                "source_field": source_field,
                "version": version,
                "confidence": confidence,
                "sample_count": sample_count,
                "normalization": normalization,
                "description": description,
                "status": "observed",
            })

        dimension(
            "workload_exposure", "任务负荷暴露",
            _score(slow.rolling_7d_workload, 0, 10)
            if slow and slow.rolling_7d_workload is not None else None,
            source="participant_slow_state",
            source_field="rolling_7d_workload",
            version=slow.created_at.isoformat() if slow else None,
            confidence=evidence_coverage,
            normalization="linear_clip_0_10_to_0_100",
        )
        parameters = dict(learned.parameters_json or {}) if learned else {}
        reactivity_key = next(
            (key for key in ("stress_reactivity_i", "stress_reactivity", "reactivity") if key in parameters),
            None,
        )
        reactivity = _number(parameters.get(reactivity_key)) if reactivity_key else None
        dimension(
            "stress_reactivity", "压力反应性",
            _score(reactivity, 0, 1.5) if reactivity is not None else None,
            source="learned_model_profile",
            source_field=reactivity_key or "stress_reactivity_i",
            version=learned.version if learned else None,
            confidence=learned.confidence if learned else None,
            normalization="linear_clip_0_1.5_per_hour_to_0_100",
            sample_count=learned.sample_count if learned else None,
        )
        dimension(
            "recovery_capacity", "恢复能力",
            _score(slow.recent_recovery_quality, 0, 10)
            if slow and slow.recent_recovery_quality is not None else None,
            source="participant_slow_state",
            source_field="recent_recovery_quality",
            version=slow.created_at.isoformat() if slow else None,
            confidence=evidence_coverage,
            normalization="linear_clip_0_10_to_0_100",
        )

        daily_workload: list[float] = []
        for forecast in window_forecasts:
            values = [
                value for value in (_number(point.get("workload")) for point in list(forecast.curve_json or []))
                if value is not None and 0 <= value <= 1
            ]
            if values:
                daily_workload.append(statistics.fmean(values))
        volatility = statistics.pstdev(daily_workload) if len(daily_workload) >= 3 else None
        dimension(
            "workload_volatility", "任务负荷波动",
            _score(volatility, 0, 0.25) if volatility is not None else None,
            source="persisted_forecast_curve",
            source_field="daily_mean(workload)",
            version="recent_14d_latest_valid_forecast_per_day",
            confidence=min(1.0, len(daily_workload) / 14),
            normalization="population_sd_linear_clip_0_0.25_to_0_100",
            sample_count=len(daily_workload),
            description="近 14 日有效 Forecast 中每日平均 W(t) 的标准差",
        )
        receptivity = [_number(value) for value in care_rows]
        receptivity = [value for value in receptivity if value is not None and 0 <= value <= 1]
        dimension(
            "receptivity", "干预可接受性",
            round(statistics.fmean(receptivity) * 100, 1) if receptivity else None,
            source="care_intervention_event",
            source_field="receptivity_score",
            version="recent_28d",
            confidence=min(1.0, len(receptivity) / 10),
            normalization="mean_linear_0_1_to_0_100",
            sample_count=len(receptivity),
        )
        dimension(
            "evidence_coverage", "数据可信度",
            round(evidence_coverage * 100, 1) if evidence_coverage is not None else None,
            source="overview_coverage_components",
            source_field="ema_day_rate,forecast_day_rate,profile,learned_sample_coverage",
            version=OVERVIEW_SCHEMA_VERSION,
            confidence=evidence_coverage,
            normalization="mean_of_available_coverage_components",
            sample_count=eligible_days,
        )

        care_effect = self.care_effects.descriptive_effects(
            start_28, through, participant_id=participant_id
        )
        care_summary = dict(care_effect.get("summary") or {})
        assessments: list[dict[str, Any]] = []
        if peak_stress is not None:
            assessments.append({
                "message": f"{through.isoformat()} Forecast 的预测峰值为 {peak_stress:.1f}/10，出现在 {str((peak_point or {}).get('time') or '未知时段')[:5]}。",
                "level": attention_level,
                "evidence_keys": ["risk_summary.peak_stress", "current_model.forecast_id"],
                "sample_count": len(valid_stress),
                "confidence": evidence_coverage,
            })
        if care_summary.get("total_sample_count", 0):
            assessments.append({
                "message": f"近 28 日 Care 共 {care_summary['total_sample_count']} 个样本，结果仅作为观察性描述证据。",
                "level": "info",
                "evidence_keys": ["care_summary", "care_summary.analysis_type"],
                "sample_count": care_summary["total_sample_count"],
                "confidence": min(1.0, care_summary["total_sample_count"] / 10),
            })
        else:
            assessments.append({
                "message": "近 28 日暂无 Care 效果样本，不能判断干预是否有帮助。",
                "level": "info",
                "evidence_keys": ["care_summary.total_sample_count"],
                "sample_count": 0,
                "confidence": 0.0,
            })
        if learned is not None:
            assessments.append({
                "message": f"当前个体参数版本 v{learned.version}，validation 状态为 {learned.validation_status}。",
                "level": "info" if learned.validation_status == "validated" else "attention",
                "evidence_keys": ["current_model.learned_profile_version", "key_parameters"],
                "sample_count": learned.sample_count,
                "confidence": learned.confidence,
            })

        latest_payload = dict(observation.payload_json or {}) if observation else {}
        latest_activity_values = [
            value for value in (
                _aware(observation.observed_at) if observation else None,
                _aware(last_message_at) if last_message_at else None,
            ) if value is not None
        ]
        key_parameters = []
        for key, raw in parameters.items():
            estimate = _number(raw)
            if estimate is None:
                continue
            uncertainty = dict(learned.uncertainty_json or {}).get(key) if learned else None
            key_parameters.append({
                "parameter": key,
                "estimate": estimate,
                "uncertainty": uncertainty,
                "version": learned.version if learned else None,
                "sample_count": learned.sample_count if learned else None,
                "validation_status": learned.validation_status if learned else None,
                "evidence_window": (
                    f"{learned.window_start.isoformat()}–{learned.window_end.isoformat()}"
                    if learned else None
                ),
            })

        return {
            "schema_version": OVERVIEW_SCHEMA_VERSION,
            "participant_code": participant.participant_code,
            "status": participant.status,
            "last_active_at": max(latest_activity_values).isoformat() if latest_activity_values else None,
            "data_quality": {
                "eligible_day_count_14d": eligible_days,
                "ema_observed_day_rate_14d": round(ema_day_rate, 4) if ema_day_rate is not None else None,
                "forecast_coverage_14d": round(forecast_day_rate, 4) if forecast_day_rate is not None else None,
                "evidence_coverage": round(evidence_coverage, 4) if evidence_coverage is not None else None,
            },
            "current_model": {
                "forecast_id": str(latest_forecast.id) if latest_forecast else None,
                "forecast_date": latest_forecast.local_date.isoformat() if latest_forecast else None,
                "forecast_version": latest_forecast.forecast_version if latest_forecast else None,
                "algorithm_version": latest_forecast.algorithm_version if latest_forecast else None,
                "model_family": dict(latest_forecast.output_json or {}).get("model_family") if latest_forecast else None,
                "learned_profile_version": learned.version if learned else None,
                "validation_status": learned.validation_status if learned else None,
            },
            "current_state": {
                "latest_observed_at": _aware(observation.observed_at).isoformat() if observation else None,
                "stress_0_10": _number(latest_payload.get("stress_0_10")),
                "energy_0_10": _number(latest_payload.get("energy_0_10")),
                "observation_type": observation.observation_type if observation else None,
            },
            "risk_summary": {
                "attention_level": attention_level,
                "peak_stress": round(peak_stress, 2) if peak_stress is not None else None,
                "peak_time": (peak_point or {}).get("time"),
                "warning_count_7d": warnings_7d,
                "label_contract": "internal_research_attention_not_diagnosis",
            },
            "profile_dimensions": dimensions,
            "key_parameters": key_parameters,
            "behavior_summary": {
                "daily_review_completion_rate_14d": round(review_rate, 4) if review_rate is not None else None,
                "daily_review_completed_days": len(set(review_days)),
                "ema_observed_days": len(observed_days),
            },
            "care_summary": {
                **care_summary,
                "analysis_type": care_effect["analysis_type"],
                "causal_claim_allowed": care_effect["causal_claim_allowed"],
                "date_start": care_effect["date_start"],
                "date_end": care_effect["date_end"],
            },
            "recent_trends": {
                "daily_workload_mean_14d": [round(value, 4) for value in daily_workload],
            },
            "system_assessment": assessments,
            "provenance": {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "through": through.isoformat(),
                "timezone": str(self.timezone),
                "schema_version": OVERVIEW_SCHEMA_VERSION,
                "clinical_diagnosis": False,
            },
        }
