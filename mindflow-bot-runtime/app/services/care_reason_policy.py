"""Deterministic selection of the few care reasons worth explaining."""

from __future__ import annotations

from typing import Any

from app.contracts.care_evidence import CareEvidencePacket, CARE_REASON_POLICY_VERSION


class CareReasonSelector:
    MAX_REASONS = 3

    def select(self, packet: CareEvidencePacket) -> tuple[dict[str, Any], ...]:
        candidates: list[dict[str, Any]] = []
        schedule = packet.schedule
        trajectory = packet.trajectory
        state = packet.recent_state
        personalization = packet.personalization
        events = list(packet.event_facts)
        high_events = [
            item for item in events
            if str(item.get("workload_level") or "") == "high"
        ]
        course_events = [
            item for item in events
            if str(item.get("event_type") or "") == "course"
        ]
        if len(course_events) >= 2 and int(schedule.get("consecutive_course_count") or 0) >= 2:
            candidates.append({
                "code": "dense_high_load_course_block",
                "fact_ids": [str(item["fact_id"]) for item in high_events[:4]],
                "salience": min(1.0, 0.72 + 0.05 * len(high_events)),
            })
        continuous = max(
            float(schedule.get("weighted_load") or 0.0),
            float(trajectory.get("continuous_load_factor") or 0.0),
        )
        if continuous >= 0.60:
            candidates.append({
                "code": "sustained_continuous_load",
                "fact_ids": [str(item["fact_id"]) for item in high_events[:3]],
                "salience": round(min(1.0, continuous), 3),
            })
        if (
            int(schedule.get("consecutive_course_count") or 0) >= 2
            and int(schedule.get("largest_break_minutes") or 999) <= 20
        ):
            candidates.append({
                "code": "insufficient_recovery_window",
                "fact_ids": [str(item["fact_id"]) for item in course_events[:3]],
                "salience": 0.74,
            })
        carryover = dict(trajectory.get("previous_day_carryover") or {})
        if carryover.get("present") is True:
            candidates.append({
                "code": "previous_day_carryover",
                "fact_ids": [],
                "salience": min(1.0, 0.60 + max(0.0, float(carryover.get("stress_above_baseline") or 0.0)) / 5.0),
            })
        if state.get("energy_tendency") == "low":
            candidates.append({"code": "low_recent_energy", "fact_ids": [], "salience": 0.61})
        if state.get("stress_tendency") == "high":
            candidates.append({"code": "high_recent_stress", "fact_ids": [], "salience": 0.60})
        if personalization.get("workload_sensitivity") == "above_population_prior":
            candidates.append({"code": "high_personal_workload_sensitivity", "fact_ids": [], "salience": 0.58})
        if personalization.get("recovery_rate") == "slower_than_population_prior":
            candidates.append({"code": "slow_personal_recovery", "fact_ids": [], "salience": 0.57})
        if len(high_events) == 1 and not course_events:
            candidates.append({
                "code": "single_high_load_event",
                "fact_ids": [str(high_events[0]["fact_id"])],
                "salience": 0.70,
            })
        if not candidates:
            candidates.append({"code": "limited_context", "fact_ids": [], "salience": 0.30})
        candidates.sort(key=lambda item: (-float(item["salience"]), str(item["code"])))
        return tuple(candidates[: self.MAX_REASONS])


__all__ = ["CARE_REASON_POLICY_VERSION", "CareReasonSelector"]
