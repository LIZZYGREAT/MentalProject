"""Stage-6 proximal outcomes, descriptive effects, and evidence-gated insights."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
import math
import statistics
from typing import Any
import uuid
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.db import Database
from app.models import (
    CareInterventionEvent,
    CareInterventionOutcome,
    ForecastSnapshot,
    InterventionRandomizationEvent,
    StateObservation,
    utc_now,
)


CARE_EFFECT_VERSION = "care-effect-descriptive.v1"
WEEKLY_INSIGHT_VERSION = "weekly-insight-evidence-gate.v1"


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _stress(payload: dict[str, Any] | None) -> float | None:
    raw = (payload or {}).get("stress_0_10")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and 0 <= value <= 10 else None


def _mean(values: list[float]) -> float | None:
    return round(statistics.fmean(values), 4) if values else None


def _uncertainty(values: list[float]) -> dict[str, float] | None:
    if not values:
        return None
    mean = statistics.fmean(values)
    se = statistics.stdev(values) / math.sqrt(len(values)) if len(values) > 1 else 0.0
    return {
        "standard_error": round(se, 4),
        "lower_95": round(mean - 1.96 * se, 4),
        "upper_95": round(mean + 1.96 * se, 4),
    }


class CareEffectivenessService:
    def __init__(self, database: Database, timezone_name: str):
        self.database = database
        self.timezone = ZoneInfo(timezone_name)

    def refresh_outcomes(
        self,
        participant_id: uuid.UUID | None = None,
        *,
        as_of: datetime | None = None,
    ) -> dict[str, int]:
        cutoff = _aware(as_of or utc_now())
        created = updated = 0
        with self.database.session() as session:
            query = select(CareInterventionEvent).where(
                CareInterventionEvent.sent_at.is_not(None),
                CareInterventionEvent.sent_at <= cutoff,
            )
            if participant_id is not None:
                query = query.where(
                    CareInterventionEvent.participant_id == participant_id
                )
            interventions = session.execute(query).scalars().all()
            for intervention in interventions:
                outcome = session.get(CareInterventionOutcome, intervention.id)
                if outcome is None:
                    context = dict(intervention.context_json or {})
                    care_context = dict(context.get("care_context") or {})
                    outcome = CareInterventionOutcome(
                        intervention_id=intervention.id,
                        participant_id=intervention.participant_id,
                        baseline_state={
                            "stress_0_10": care_context.get("stress_0_10"),
                            "vitality_0_10": care_context.get("vitality_0_10"),
                            "measured_at": care_context.get("risk_time"),
                            "source": "forecast_at_decision_time",
                        },
                        context_json={
                            "observational_only": True,
                            "intervention_type": intervention.intervention_type,
                            "receptivity_score": intervention.receptivity_score,
                        },
                        created_at=cutoff,
                        updated_at=cutoff,
                    )
                    session.add(outcome)
                    created += 1
                changed = self._match_followups(session, intervention, outcome, cutoff)
                if changed:
                    updated += 1
            session.flush()
        return {"created": created, "updated": updated}

    @staticmethod
    def _match_followups(
        session: Any,
        intervention: CareInterventionEvent,
        outcome: CareInterventionOutcome,
        cutoff: datetime,
    ) -> bool:
        sent_at = _aware(intervention.sent_at)
        observations = session.execute(
            select(StateObservation).where(
                StateObservation.participant_id == intervention.participant_id,
                StateObservation.observed_at > sent_at,
                StateObservation.observed_at <= sent_at + timedelta(minutes=75),
                StateObservation.observed_at <= cutoff,
                StateObservation.created_at <= cutoff,
                StateObservation.observation_type == "checkin",
            ).order_by(StateObservation.observed_at)
        ).scalars().all()
        baseline = _stress(dict(outcome.baseline_state or {}))

        def nearest(minutes: int, lower: int, upper: int) -> dict[str, Any] | None:
            candidates = [
                row for row in observations
                if lower <= (_aware(row.observed_at) - sent_at).total_seconds() / 60 <= upper
                and _stress(dict(row.payload_json or {})) is not None
            ]
            if not candidates:
                return None
            row = min(
                candidates,
                key=lambda item: abs(
                    (_aware(item.observed_at) - sent_at).total_seconds() / 60 - minutes
                ),
            )
            value = _stress(dict(row.payload_json or {}))
            return {
                "observation_id": str(row.id),
                "observed_at": _aware(row.observed_at).isoformat(),
                "stress_0_10": value,
                "stress_change": round(value - baseline, 4)
                if value is not None and baseline is not None else None,
                "available_at": _aware(row.created_at).isoformat(),
            }

        changed = False
        followup_30 = nearest(30, 15, 44.9999)
        followup_60 = nearest(60, 45, 75)
        if followup_30 and dict(outcome.followup_30m or {}) != followup_30:
            outcome.followup_30m = followup_30
            changed = True
        if followup_60 and dict(outcome.followup_60m or {}) != followup_60:
            outcome.followup_60m = followup_60
            changed = True
        if outcome.user_action != intervention.user_action and intervention.user_action:
            outcome.user_action = intervention.user_action
            changed = True
        if changed:
            outcome.updated_at = cutoff
        return changed

    def descriptive_effects(
        self,
        date_start: date,
        date_end: date,
        *,
        participant_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        self.refresh_outcomes(participant_id)
        start = datetime.combine(date_start, time.min, self.timezone).astimezone(timezone.utc)
        end = datetime.combine(date_end + timedelta(days=1), time.min, self.timezone).astimezone(timezone.utc)
        with self.database.session() as session:
            query = select(CareInterventionEvent, CareInterventionOutcome).join(
                CareInterventionOutcome,
                CareInterventionOutcome.intervention_id == CareInterventionEvent.id,
            ).where(
                CareInterventionEvent.sent_at >= start,
                CareInterventionEvent.sent_at < end,
            )
            if participant_id is not None:
                query = query.where(
                    CareInterventionEvent.participant_id == participant_id
                )
            rows = session.execute(query).all()
            mrt_count = session.scalar(
                select(InterventionRandomizationEvent.id).limit(1)
            )
        grouped: dict[tuple[str, str, str, str], list[tuple[Any, Any]]] = defaultdict(list)
        for intervention, outcome in rows:
            baseline = _stress(dict(outcome.baseline_state or {}))
            stress_band = "unknown" if baseline is None else "high" if baseline >= 7 else "medium" if baseline >= 4 else "low"
            context = dict(outcome.context_json or {}).get("care_context") or dict(intervention.context_json or {}).get("care_context") or {}
            workload = "high" if context.get("current_events") or context.get("dominant_stressors") else "low"
            local_hour = _aware(intervention.sent_at).astimezone(self.timezone).hour
            period = "morning" if local_hour < 12 else "afternoon" if local_hour < 18 else "evening"
            grouped[(intervention.intervention_type, stress_band, workload, period)].append((intervention, outcome))
        groups = []
        for (kind, stress_band, workload, period), items in sorted(grouped.items()):
            ratings = [float(outcome.helpful_rating) for _, outcome in items if outcome.helpful_rating is not None]
            delta30 = [float(outcome.followup_30m["stress_change"]) for _, outcome in items if outcome.followup_30m and outcome.followup_30m.get("stress_change") is not None]
            delta60 = [float(outcome.followup_60m["stress_change"]) for _, outcome in items if outcome.followup_60m and outcome.followup_60m.get("stress_change") is not None]
            receptivity = [float(item.receptivity_score) for item, _ in items if item.receptivity_score is not None]
            groups.append({
                "intervention_type": kind,
                "stress_level": stress_band,
                "workload": workload,
                "time_of_day": period,
                "sample_count": len(items),
                "helpful_sample_count": len(ratings),
                "helpful_rate": _mean(ratings),
                "stress_change_30m": _mean(delta30),
                "stress_change_60m": _mean(delta60),
                "receptivity": _mean(receptivity),
                "uncertainty": {
                    "helpful_rate": _uncertainty(ratings),
                    "stress_change_30m": _uncertainty(delta30),
                    "stress_change_60m": _uncertainty(delta60),
                },
                "causal_effect": None,
            })
        return {
            "schema_version": CARE_EFFECT_VERSION,
            "analysis_type": "observational_descriptive",
            "causal_claim_allowed": False,
            "mrt_runtime_enabled": False,
            "mrt_rows_present": bool(mrt_count),
            "date_start": date_start.isoformat(),
            "date_end": date_end.isoformat(),
            "groups": groups,
        }

    def weekly_insights(
        self,
        participant_id: uuid.UUID,
        *,
        through: date,
        minimum_sample_count: int = 5,
    ) -> dict[str, Any]:
        start = through - timedelta(days=6)
        effects = self.descriptive_effects(start, through, participant_id=participant_id)
        with self.database.session() as session:
            forecasts = session.execute(
                select(ForecastSnapshot).where(
                    ForecastSnapshot.participant_id == participant_id,
                    ForecastSnapshot.local_date >= start,
                    ForecastSnapshot.local_date <= through,
                    ForecastSnapshot.valid.is_(True),
                ).order_by(ForecastSnapshot.local_date)
            ).scalars().all()
        daily_peaks: list[float] = []
        daily_workload: list[float] = []
        recovery_changes: list[float] = []
        for forecast in forecasts:
            curve = list(forecast.curve_json or [])
            stresses = [float(point.get("stress_0_10") or 0.0) for point in curve]
            if stresses:
                daily_peaks.append(max(stresses))
            workloads = [float(point.get("workload") or 0.0) for point in curve]
            if workloads:
                daily_workload.append(statistics.fmean(workloads))
            for left, right in zip(curve, curve[1:]):
                if float(left.get("recovery_resource") or 0.0) >= 0.35:
                    recovery_changes.append(
                        float(right.get("stress_0_10") or 0.0)
                        - float(left.get("stress_0_10") or 0.0)
                    )
        candidates = [
            ("pressure_pattern", daily_peaks, "本周每日预测压力峰值的平均水平"),
            ("workload_pattern", daily_workload, "本周每日平均 workload"),
            ("recovery_pattern", recovery_changes, "恢复窗口后一个时间步的平均压力变化"),
        ]
        for group in effects["groups"]:
            ratings_count = int(group["helpful_sample_count"])
            if ratings_count:
                candidates.append((
                    f"care_feedback:{group['intervention_type']}",
                    [float(group["helpful_rate"])] * ratings_count,
                    f"{group['intervention_type']} 的有帮助反馈率",
                ))
        insights = []
        suppressed = []
        for kind, values, statement in candidates:
            payload = {
                "insight_type": kind,
                "sample_count": len(values),
                "effect_estimate": _mean(values),
                "uncertainty": _uncertainty(values),
                "statement": statement,
                "evidence_type": "observational",
            }
            (insights if len(values) >= minimum_sample_count else suppressed).append(payload)
        return {
            "schema_version": WEEKLY_INSIGHT_VERSION,
            "date_start": start.isoformat(),
            "date_end": through.isoformat(),
            "minimum_sample_count": minimum_sample_count,
            "insights": insights,
            "suppressed": suppressed,
        }
