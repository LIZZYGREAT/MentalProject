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


CARE_EFFECT_VERSION = "care-effect-descriptive.v2"
WEEKLY_INSIGHT_VERSION = "weekly-insight-evidence-gate.v2"


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


def _wilson_uncertainty(values: list[float]) -> dict[str, Any] | None:
    """Wilson interval for Bernoulli helpfulness observations."""

    if not values:
        return None
    n = len(values)
    mean = statistics.fmean(values)
    z = 1.96
    denominator = 1.0 + (z * z / n)
    centre = (mean + z * z / (2.0 * n)) / denominator
    margin = (
        z
        * math.sqrt((mean * (1.0 - mean) / n) + (z * z / (4.0 * n * n)))
        / denominator
    )
    return {
        "standard_error": round(math.sqrt(mean * (1.0 - mean) / n), 4),
        "lower_95": round(max(0.0, centre - margin), 4),
        "upper_95": round(min(1.0, centre + margin), 4),
        "method": "wilson",
    }


def _baseline_values(outcome: CareInterventionOutcome) -> tuple[float | None, float | None]:
    baseline = dict(outcome.baseline_state or {})
    predicted = dict(baseline.get("predicted_baseline") or {})
    observed = dict(baseline.get("observed_baseline") or {})
    # Legacy fallback is read-only compatibility for already materialized rows.
    predicted_stress = _stress(predicted) if predicted else _stress(baseline)
    observed_stress = _stress(observed) if observed else None
    return predicted_stress, observed_stress


def _recovery_episode_changes(curve: list[dict[str, Any]]) -> list[float]:
    """Return one pre/post stress delta per contiguous recovery episode."""

    changes: list[float] = []
    index = 0
    while index < len(curve):
        if float(curve[index].get("recovery_resource") or 0.0) < 0.35:
            index += 1
            continue
        start = index
        while (
            index + 1 < len(curve)
            and float(curve[index + 1].get("recovery_resource") or 0.0) >= 0.35
        ):
            index += 1
        end = index
        if start == 0 or end + 1 >= len(curve):
            index += 1
            continue
        pre_index = start - 1
        post_index = end + 1
        try:
            before = float(curve[pre_index].get("stress_0_10"))
            after = float(curve[post_index].get("stress_0_10"))
        except (TypeError, ValueError):
            pass
        else:
            if math.isfinite(before) and math.isfinite(after):
                changes.append(after - before)
        index += 1
    return changes


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
                    from app.repositories_care import CareInterventionRepository

                    outcome = CareInterventionRepository._ensure_outcome_in_session(
                        session, intervention, cutoff
                    )
                    created += 1
                changed = self._match_followups(session, intervention, outcome, cutoff)
                if changed:
                    updated += 1
            session.flush()
        return {"created": created, "updated": updated}

    @staticmethod
    def attach_observation_in_session(
        session: Any, observation: StateObservation
    ) -> int:
        """Materialize eligible follow-ups when a check-in is committed."""

        if observation.observation_type != "checkin":
            return 0
        observed_at = _aware(observation.observed_at)
        cutoff = _aware(observation.created_at)
        interventions = session.execute(
            select(CareInterventionEvent).where(
                CareInterventionEvent.participant_id == observation.participant_id,
                CareInterventionEvent.sent_at.is_not(None),
                CareInterventionEvent.sent_at < observed_at,
                CareInterventionEvent.sent_at >= observed_at - timedelta(minutes=75),
            )
        ).scalars().all()
        changed = 0
        from app.repositories_care import CareInterventionRepository

        for intervention in interventions:
            outcome = CareInterventionRepository._ensure_outcome_in_session(
                session, intervention, cutoff
            )
            if CareEffectivenessService._match_followups(
                session, intervention, outcome, cutoff
            ):
                changed += 1
        return changed

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
        predicted_baseline, observed_baseline = _baseline_values(outcome)

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
            energy = (row.payload_json or {}).get("energy_0_10")
            return {
                "observation_id": str(row.id),
                "observed_at": _aware(row.observed_at).isoformat(),
                "stress_0_10": value,
                "energy_0_10": energy,
                "observed_stress_change": round(value - observed_baseline, 4)
                if value is not None and observed_baseline is not None else None,
                "forecast_residual": round(value - predicted_baseline, 4)
                if value is not None and predicted_baseline is not None else None,
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
        knowledge_cutoff: datetime | None = None,
    ) -> dict[str, Any]:
        start = datetime.combine(date_start, time.min, self.timezone).astimezone(timezone.utc)
        end = datetime.combine(date_end + timedelta(days=1), time.min, self.timezone).astimezone(timezone.utc)
        cutoff = (
            _aware(knowledge_cutoff).astimezone(timezone.utc)
            if knowledge_cutoff is not None
            else None
        )
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
            if cutoff is not None:
                query = query.where(
                    CareInterventionEvent.sent_at < cutoff,
                    CareInterventionEvent.created_at < cutoff,
                    CareInterventionOutcome.created_at < cutoff,
                    CareInterventionOutcome.updated_at < cutoff,
                )
            rows = session.execute(query).all()
            mrt_query = select(InterventionRandomizationEvent.id).where(
                InterventionRandomizationEvent.decision_time >= start,
                InterventionRandomizationEvent.decision_time < end,
            )
            if participant_id is not None:
                mrt_query = mrt_query.where(
                    InterventionRandomizationEvent.participant_id == participant_id
                )
            if cutoff is not None:
                mrt_query = mrt_query.where(
                    InterventionRandomizationEvent.created_at < cutoff
                )
            mrt_count = session.scalar(mrt_query.limit(1))
        grouped: dict[tuple[str, str, str, str], list[tuple[Any, Any]]] = defaultdict(list)
        for intervention, outcome in rows:
            baseline, _observed = _baseline_values(outcome)
            stress_band = "unknown" if baseline is None else "high" if baseline >= 7 else "medium" if baseline >= 4 else "low"
            context = dict(outcome.context_json or {}).get("care_context") or dict(intervention.context_json or {}).get("care_context") or {}
            workload = "high" if context.get("current_events") or context.get("dominant_stressors") else "low"
            local_hour = _aware(intervention.sent_at).astimezone(self.timezone).hour
            period = "morning" if local_hour < 12 else "afternoon" if local_hour < 18 else "evening"
            grouped[(intervention.intervention_type, stress_band, workload, period)].append((intervention, outcome))
        groups = []
        all_ratings: list[float] = []
        all_receptivity: list[float] = []
        followup_30m_count = 0
        followup_60m_count = 0
        for (kind, stress_band, workload, period), items in sorted(grouped.items()):
            ratings = [float(outcome.helpful_rating) for _, outcome in items if outcome.helpful_rating is not None]
            delta30 = [float(outcome.followup_30m["observed_stress_change"]) for _, outcome in items if outcome.followup_30m and outcome.followup_30m.get("observed_stress_change") is not None]
            delta60 = [float(outcome.followup_60m["observed_stress_change"]) for _, outcome in items if outcome.followup_60m and outcome.followup_60m.get("observed_stress_change") is not None]
            residual30 = [float(outcome.followup_30m["forecast_residual"]) for _, outcome in items if outcome.followup_30m and outcome.followup_30m.get("forecast_residual") is not None]
            residual60 = [float(outcome.followup_60m["forecast_residual"]) for _, outcome in items if outcome.followup_60m and outcome.followup_60m.get("forecast_residual") is not None]
            receptivity = [float(item.receptivity_score) for item, _ in items if item.receptivity_score is not None]
            all_ratings.extend(ratings)
            all_receptivity.extend(receptivity)
            followup_30m_count += len(delta30)
            followup_60m_count += len(delta60)
            groups.append({
                "intervention_type": kind,
                "stress_level": stress_band,
                "workload": workload,
                "time_of_day": period,
                "sample_count": len(items),
                "helpful_sample_count": len(ratings),
                "helpful_count": int(sum(ratings)),
                "followup_30m_sample_count": len(delta30),
                "followup_60m_sample_count": len(delta60),
                "helpful_rate": _mean(ratings),
                "helpful_binary_observations": ratings,
                "observed_stress_change_30m": _mean(delta30),
                "observed_stress_change_60m": _mean(delta60),
                "forecast_residual_30m": _mean(residual30),
                "forecast_residual_60m": _mean(residual60),
                "receptivity": _mean(receptivity),
                "uncertainty": {
                    "helpful_rate": _wilson_uncertainty(ratings),
                    "observed_stress_change_30m": _uncertainty(delta30),
                    "observed_stress_change_60m": _uncertainty(delta60),
                    "forecast_residual_30m": _uncertainty(residual30),
                    "forecast_residual_60m": _uncertainty(residual60),
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
            "summary": {
                # The grouping keys form a mutually-exclusive partition, so
                # these totals do not double count interventions.
                "group_count": len(groups),
                "total_sample_count": len(rows),
                "helpful_sample_count": len(all_ratings),
                "helpful_count": int(sum(all_ratings)),
                "overall_helpful_rate": _mean(all_ratings),
                "followup_30m_sample_count": followup_30m_count,
                "followup_60m_sample_count": followup_60m_count,
                "mean_receptivity": _mean(all_receptivity),
            },
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
            recovery_changes.extend(_recovery_episode_changes(curve))
        candidates = [
            {
                "kind": "pressure_pattern", "values": daily_peaks,
                "statement": "本周每日预测压力峰值的平均水平",
                "uncertainty_method": "normal_mean",
            },
            {
                "kind": "workload_pattern", "values": daily_workload,
                "statement": "本周每日平均 workload",
                "uncertainty_method": "normal_mean",
            },
            {
                "kind": "recovery_pattern", "values": recovery_changes,
                "statement": "每个连续恢复 episode 前后的平均压力变化",
                "uncertainty_method": "normal_mean",
            },
        ]
        for group in effects["groups"]:
            ratings = [
                float(value)
                for value in group.get("helpful_binary_observations") or []
            ]
            if ratings:
                candidates.append({
                    "kind": f"care_feedback:{group['intervention_type']}",
                    "values": ratings,
                    "statement": f"{group['intervention_type']} 的有帮助反馈率",
                    "uncertainty_method": "wilson",
                })
        insights = []
        suppressed = []
        for candidate in candidates:
            kind = str(candidate["kind"])
            values = list(candidate["values"])
            method = str(candidate["uncertainty_method"])
            payload = {
                "insight_type": kind,
                "sample_count": len(values),
                "effect_estimate": _mean(values),
                "uncertainty": (
                    _wilson_uncertainty(values)
                    if method == "wilson"
                    else _uncertainty(values)
                ),
                "uncertainty_method": method,
                "statement": str(candidate["statement"]),
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
