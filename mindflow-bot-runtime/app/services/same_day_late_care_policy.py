"""Severity-preserving policy for factually re-evaluated same-day late care."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SameDayLateCarePlan:
    intervention_type: str
    template_id: str
    reason_code: str
    message: str


class SameDayLateCarePolicy:
    """Select late care without weakening a still-relevant high-tier warning."""

    @staticmethod
    def _level(value: Any) -> int:
        normalized = str(value or "").strip().casefold()
        if normalized in {"3", "red", "critical"}:
            return 3
        if normalized in {"2", "orange", "high"}:
            return 2
        return 1

    def plan(
        self,
        *,
        source_warning_level: Any,
        source_care_plan: dict[str, Any] | None,
        current_context: dict[str, Any],
    ) -> SameDayLateCarePlan:
        source_plan = dict(source_care_plan or {})
        level = self._level(source_warning_level)
        source_type = str(source_plan.get("intervention_type") or "")
        if level >= 3 or source_type == "pause_and_seek_support":
            return SameDayLateCarePlan(
                intervention_type="pause_and_seek_support",
                template_id="pause-and-support-v1",
                reason_code="missed_high_tier_same_day_care",
                message=(
                    "刚才的高压力时段提醒没有及时送达。如果你现在仍感到明显吃力，"
                    "建议先暂停手头任务，留出约 10 分钟确认自己的感受，并联系一位"
                    "你信任的人获得支持；如果当前感觉还好，可以忽略这条提醒。"
                ),
            )
        return SameDayLateCarePlan(
            intervention_type="brief_check_in",
            template_id="same-day-late-care-v1",
            reason_code="missed_proactive_same_day_care",
            message=(
                "刚才这一时段的安排比较密集。如果你现在仍在连续处理任务，"
                "可以先留几分钟缓冲，喝口水或短暂离开屏幕。"
            ),
        )
