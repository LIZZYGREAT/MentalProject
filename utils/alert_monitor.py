"""Burden-aware care policy for predicted subjective stress.

The monitor separates *state estimation* from *intervention delivery*.  It
detects persistent candidate episodes only.  Cross-episode ranking, the daily
budget and delivery interval belong to the runtime WarningPolicy and durable
repository guard, so a model-side exception can never bypass user limits.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from algorithm.time_utils import time_to_minutes
from settings.model_defaults import DEFAULT_ENERGY_CRITICAL, RECOVERY_STATES


def _clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(upper, float(value)))


class AlertMonitor:
    """Convert a predicted trajectory into a restrained daily care schedule."""

    def __init__(self, params: Dict[str, Any]):
        self.params = params
        cfg = params.get("alert_thresholds", {})
        baseline = float(params.get("S_star_init", 50.0))
        self.yellow_stress = min(
            85.0,
            max(float(cfg.get("yellow_stress", 70.0)), baseline + 12.0),
        )
        self.orange_stress = min(
            93.0,
            max(float(cfg.get("orange_stress", 80.0)), self.yellow_stress + 8.0),
        )
        self.red_stress = min(
            98.0,
            max(float(cfg.get("red_stress", 88.0)), self.orange_stress + 8.0),
        )
        self.extreme_stress = min(
            100.0,
            max(float(cfg.get("extreme_stress", 94.0)), self.red_stress + 2.0),
        )
        self.recovery_stress = min(
            self.yellow_stress - 3.0,
            max(float(cfg.get("recovery_stress", 62.0)), baseline + 8.0),
        )
        self.confirm_minutes = {
            1: float(cfg.get("yellow_confirm_minutes", 40.0)),
            2: float(cfg.get("orange_confirm_minutes", 20.0)),
            3: float(cfg.get("red_confirm_minutes", 10.0)),
        }
        self.rearm_minutes = float(cfg.get("rearm_minutes", 45.0))
        self.auc_thresholds = {
            1: float(cfg.get("elevated_auc_yellow", 2.2)),
            2: float(cfg.get("elevated_auc_orange", 3.6)),
            3: float(cfg.get("elevated_auc_red", 5.5)),
        }
        self.vitality_danger = float(
            cfg.get("E_danger", params.get("E_critical", DEFAULT_ENERGY_CRITICAL))
        )
        self.default_step = max(1.0, float(params.get("time_step", 5.0)))

    def analyze(self, results: List[Dict[str, Any]]) -> Tuple[List[Dict], List[float]]:
        alerts: List[Dict[str, Any]] = []
        confidence_series: List[float] = []
        if not results:
            return alerts, confidence_series

        elevated_auc = 0.0
        candidate_tier = 0
        candidate_minutes = 0.0
        episode_tier = 0
        recovery_minutes = 0.0
        episode_index = 0
        previous_minute: Optional[int] = None

        for row in results:
            minute = time_to_minutes(row.get("time", "00:00"))
            if previous_minute is None:
                dt_minutes = self.default_step
            else:
                dt_minutes = float((minute - previous_minute) % 1440)
                if dt_minutes <= 0.0:
                    dt_minutes = self.default_step
            previous_minute = minute
            dt_hours = dt_minutes / 60.0

            stress = float(row.get("S", 0.0))
            vitality = float(row.get("V", row.get("E", 72.0)))
            fatigue = _clamp(row.get("F", row.get("recovery_debt", 0.0)))
            delta_stress = float(row.get("delta_S", 0.0))
            state = str(row.get("state", "UNKNOWN"))
            sleeping = state in RECOVERY_STATES
            recovering = bool(
                row.get("recovery_input", 0.0) >= 0.35 and delta_stress < 0.0
            )

            if sleeping:
                elevated_auc = max(0.0, elevated_auc - 1.8 * dt_hours)
            elif stress > self.recovery_stress:
                # Unit: hours at ten points above the recovery line.
                elevated_auc += (
                    (stress - self.recovery_stress) / 10.0
                ) * dt_hours
            else:
                recovery_strength = 0.8 + max(
                    0.0, (self.recovery_stress - stress) / 10.0
                )
                elevated_auc = max(
                    0.0, elevated_auc - recovery_strength * dt_hours
                )

            intensity_tier = self._intensity_tier(stress)
            burden_tier = self._burden_tier(elevated_auc)
            vulnerability_tier = 0
            if stress >= self.recovery_stress + 3.0 and (
                fatigue >= 0.68 or vitality <= self.vitality_danger
            ):
                vulnerability_tier = 2 if (
                    fatigue >= 0.82 or vitality <= self.vitality_danger - 8.0
                ) else 1

            # Accumulated burden can justify a stronger check-in, but a red
            # user-facing label is reserved for a genuinely red current
            # pressure estimate.  This keeps wording aligned with the value.
            target_tier = max(
                intensity_tier,
                min(burden_tier, 2),
                vulnerability_tier,
            )
            # Burden is useful for internal risk tracking, but a user-facing
            # message should not be emitted merely because the three-hour
            # cooldown elapsed while pressure is already falling below the
            # yellow band.  This was the cause of mechanically repeated care
            # after short meal/recovery windows.
            burden_only = (
                burden_tier > 0
                and intensity_tier == 0
                and vulnerability_tier == 0
            )
            if burden_only and (
                stress < self.yellow_stress - 1.0 or delta_stress <= 0.0
            ):
                target_tier = 0
            # Do not interrupt sleep, and let an explicitly restorative period
            # finish unless the predicted intensity is already high.
            if sleeping or (recovering and intensity_tier < 2):
                target_tier = 0

            risk = self._risk_score(
                stress=stress,
                vitality=vitality,
                fatigue=fatigue,
                elevated_auc=elevated_auc,
                delta_stress=delta_stress,
            )
            if sleeping:
                risk = 0.0
            elif recovering and intensity_tier < 2:
                risk *= 0.40
            confidence_series.append(risk)

            if target_tier <= 0:
                candidate_tier = 0
                candidate_minutes = 0.0
                # A short dip or a restorative event above the recovery line
                # does not start a new pressure episode.  Rearm only after a
                # genuine sustained return to the user's recovery zone.
                if sleeping or stress <= self.recovery_stress:
                    recovery_minutes += dt_minutes
                else:
                    recovery_minutes = 0.0
                if recovery_minutes >= self.rearm_minutes:
                    episode_tier = 0
                continue

            recovery_minutes = 0.0
            if target_tier != candidate_tier:
                candidate_tier = target_tier
                candidate_minutes = dt_minutes
            else:
                candidate_minutes += dt_minutes

            extreme_override = stress >= self.extreme_stress
            confirmed = (
                extreme_override
                or candidate_minutes >= self.confirm_minutes[target_tier]
            )
            if not confirmed or target_tier <= episode_tier:
                continue

            # One supportive nudge is enough for a single uninterrupted
            # moderate-load episode.  A second message in that episode is only
            # justified by a genuinely higher current stress band (or a clear
            # combined low-vitality/high-debt escalation), not by the passage
            # of time alone.
            if episode_tier > 0 and target_tier > episode_tier:
                escalation_supported = (
                    intensity_tier > episode_tier
                    or (
                        vulnerability_tier > episode_tier
                        and intensity_tier >= 1
                    )
                )
                if not escalation_supported:
                    continue

            trigger_source = self._trigger_source(
                extreme_override,
                intensity_tier,
                burden_tier,
                vulnerability_tier,
            )
            if episode_tier == 0:
                episode_index += 1
            alert = self._build_alert(
                row=row,
                tier=target_tier,
                risk=risk,
                trigger_source=trigger_source,
                elevated_auc=elevated_auc,
                fatigue=fatigue,
                vitality=vitality,
                episode_index=episode_index,
            )
            alerts.append(alert)
            episode_tier = target_tier

        return alerts, confidence_series

    def _intensity_tier(self, stress: float) -> int:
        if stress >= self.red_stress:
            return 3
        if stress >= self.orange_stress:
            return 2
        if stress >= self.yellow_stress:
            return 1
        return 0

    def _burden_tier(self, elevated_auc: float) -> int:
        if elevated_auc >= self.auc_thresholds[3]:
            return 3
        if elevated_auc >= self.auc_thresholds[2]:
            return 2
        if elevated_auc >= self.auc_thresholds[1]:
            return 1
        return 0

    def _risk_score(
        self,
        *,
        stress: float,
        vitality: float,
        fatigue: float,
        elevated_auc: float,
        delta_stress: float,
    ) -> float:
        stress_component = _clamp(
            (stress - (self.recovery_stress - 6.0))
            / max(1.0, self.red_stress - (self.recovery_stress - 6.0))
        )
        exposure_component = _clamp(
            elevated_auc / max(0.1, self.auc_thresholds[3])
        )
        vitality_component = _clamp(
            (55.0 - vitality) / 40.0
        )
        trend_component = _clamp(delta_stress / 2.0)
        return _clamp(
            0.56 * stress_component
            + 0.18 * exposure_component
            + 0.14 * fatigue
            + 0.08 * vitality_component
            + 0.04 * trend_component
        )

    @staticmethod
    def _trigger_source(
        extreme: bool,
        intensity_tier: int,
        burden_tier: int,
        vulnerability_tier: int,
    ) -> str:
        if extreme:
            return "extreme_spike"
        if vulnerability_tier >= max(intensity_tier, burden_tier):
            return "combined_vulnerability"
        if burden_tier > intensity_tier:
            return "load_buildup"
        return "sustained_intensity"

    def _build_alert(
        self,
        *,
        row: Dict[str, Any],
        tier: int,
        risk: float,
        trigger_source: str,
        elevated_auc: float,
        fatigue: float,
        vitality: float,
        episode_index: int,
    ) -> Dict[str, Any]:
        if tier == 3:
            title = "[红] 很高压力趋势"
            message = "建议先暂停手头任务，确认自己的感受，并考虑联系可信任的人获得支持。"
            action = "pause_and_seek_support"
        elif tier == 2:
            title = "[橙] 持续高压提醒"
            message = "如果条件允许，安排 10–15 分钟真正脱离任务的休息，再决定下一步。"
            action = "protected_break"
        else:
            title = "[黄] 压力偏高提醒"
            message = "可以用几分钟检查任务优先级、补水或活动一下；若不需要，也可忽略本次提示。"
            action = "brief_check_in"

        current_events = list(row.get("current_events", []))
        dominant_stressors = list(row.get("dominant_stressors", []))
        return {
            "type": title,
            "message": message,
            "care_action": action,
            "time": row.get("time", "00:00"),
            "S": round(float(row.get("S", 0.0)), 2),
            "V": round(vitality, 2),
            "E": round(vitality, 2),
            "P": round(float(row.get("P", 0.0)), 3),
            "F": round(fatigue, 3),
            "state": row.get("state", "UNKNOWN"),
            "trigger_source": trigger_source,
            "intensity_zone": ("critical" if tier == 3 else "high" if tier == 2 else "elevated"),
            "continuous_hours": round(
                float(row.get("continuous_hours", 0.0)), 2
            ),
            "elevated_auc": round(elevated_auc, 3),
            "current_events": current_events,
            "dominant_stressors": dominant_stressors,
            "C": round(risk, 3),
            "tier": tier,
            "episode_index": episode_index,
            "policy": {
                "persistence_confirmed": True,
                "daily_budgeted": False,
                "episode_deduplicated": True,
                "candidate_only": True,
                "clinical_alert": False,
            },
        }
