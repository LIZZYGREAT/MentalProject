"""Reviewed, versioned templates for contextual care messages."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from app.services.care_context import CareContext
from app.services.care_intervention_policy import CareMessagePlan


CARE_TEMPLATE_LIBRARY_VERSION = "care_template_library.v3"


@dataclass(frozen=True)
class RenderedCareMessage:
    message: str
    template_id: str
    template_version: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class CareTemplateLibrary:
    TEMPLATE_VERSIONS = {
        "generic-fallback-v1": "1.0.0",
        "pause-and-support-v1": "1.0.0",
        "recovery-v1": "1.0.0",
        "transition-buffer-v1": "1.0.0",
        "workload-decomposition-v1": "1.0.0",
        "protected-break-v1": "1.0.0",
        "micro-break-v1": "1.0.0",
        "brief-check-in-v1": "1.0.0",
        "schedule-adjustment-v1": "1.0.0",
    }

    def render(
        self, context: CareContext, plan: CareMessagePlan
    ) -> RenderedCareMessage:
        context_line = self._context_line(context)
        action = self._action_line(context, plan)
        choice = (
            "这只是根据日程和近期状态做出的趋势提醒；如果你现在感觉还好，可以直接忽略。"
            if context.recent_observation
            else "这只是根据今天安排做出的趋势提醒；如果你现在感觉还好，可以直接忽略。"
        )
        message = f"{context_line}\n\n{action}{choice}"
        if len(message) > 220:
            message = f"{context_line}\n\n{action}如果当前感觉还好，可以直接忽略。"
        return RenderedCareMessage(
            message=message,
            template_id=plan.template_id,
            template_version=self.TEMPLATE_VERSIONS[plan.template_id],
        )

    @staticmethod
    def _clock(value: str) -> str:
        try:
            return datetime.fromisoformat(value).strftime("%H:%M")
        except (TypeError, ValueError):
            return "稍后"

    @staticmethod
    def _name(event: dict[str, Any] | None) -> str:
        return str((event or {}).get("display_name") or "一项安排")[:36]

    def _context_line(self, context: CareContext) -> str:
        risk = self._clock(context.risk_time)
        active = context.active_event
        following = context.next_event
        previous = context.previous_event
        state_phrase = ""
        if context.profile_summary.recent_energy_tendency == "low":
            state_phrase = "；最近一次状态反馈显示精力偏低"
        elif context.profile_summary.recent_stress_tendency == "high":
            state_phrase = "；最近一次状态反馈显示压力偏高"
        elif context.profile_fact_used:
            if context.profile_summary.recovery_preference:
                state_phrase = "；建议会优先采用你记录的恢复偏好"
            elif context.profile_summary.care_preference:
                state_phrase = "；建议会按你记录的关怀偏好呈现"
            elif context.profile_summary.support_preference:
                state_phrase = "；建议会保留你记录的支持偏好"

        if active and following:
            return (
                f"模型预计 {risk} 前后压力可能上升。你当时在{self._name(active)}，"
                f"之后 {self._clock(str(following['start_time']))} 还有{self._name(following)}"
                f"{state_phrase}。"
            )
        if previous and following:
            return (
                f"模型预计 {risk} 前后压力可能上升。{self._name(previous)}结束后，"
                f"{self._clock(str(following['start_time']))} 还有{self._name(following)}"
                f"{state_phrase}。"
            )
        if following:
            return (
                f"模型预计 {risk} 前后压力可能上升，接下来 "
                f"{self._clock(str(following['start_time']))} 有{self._name(following)}"
                f"{state_phrase}。"
            )
        if active:
            return (
                f"模型预计 {risk} 前后压力可能上升，当时的安排是{self._name(active)}"
                f"{state_phrase}。"
            )
        workload = list(context.current_events or context.dominant_stressors)
        if workload:
            return (
                f"模型预计 {risk} 前后压力可能上升，可能与{workload[0][:36]}这段安排有关"
                f"{state_phrase}。"
            )
        if state_phrase:
            return f"模型预计 {risk} 前后压力可能上升{state_phrase}。"
        return f"模型预计 {risk} 前后可能出现一段压力偏高的时段，但当前可用上下文较少。"

    def _action_line(self, context: CareContext, plan: CareMessagePlan) -> str:
        if plan.intervention_type == "pause_and_seek_support":
            preference = context.profile_summary.support_preference
            preferred = (
                f"可以按你记录的偏好，{preference[:24]}。"
                if preference
                else "也可以联系一位你信任的人获得支持。"
            )
            return f"建议先暂停手头任务，留出约 {plan.action_minutes} 分钟确认自己的感受；{preferred}"
        if plan.intervention_type == "recovery":
            preference = context.profile_summary.recovery_preference
            preferred = (
                f"优先用你记录过的恢复方式“{preference[:24]}”"
                if preference
                else "先完全离开任务、补水或简单走动"
            )
            return (
                f"下一项安排前，建议留出约 {plan.action_minutes} 分钟，{preferred}，"
                "再决定下一步。"
            )
        if plan.intervention_type == "transition_buffer":
            transition = (
                f"先用你记录的恢复方式“{context.profile_summary.recovery_preference[:24]}”"
                if context.profile_summary.recovery_preference
                else "先补水或走动一下"
            )
            return (
                f"如果中间能留出 {max(5, plan.action_minutes - 5)}–{plan.action_minutes} 分钟，"
                f"{transition}，再只确认下一项最需要处理的第一件事。"
            )
        if plan.intervention_type == "workload_decomposition":
            return (
                f"可以先只确定一个 {plan.action_minutes} 分钟内能推进的小任务，"
                "其余待办暂时放到后面，避免同时盯着所有事情。"
            )
        if plan.intervention_type == "schedule_adjustment":
            return (
                f"如果你愿意，可以考虑给相邻安排留出约 {plan.action_minutes} 分钟缓冲，"
                "或把其中一项移到压力较低的时段；是否调整由你决定。"
            )
        if plan.intervention_type == "protected_break":
            recovery = (
                f"，可以采用你记录的恢复方式“{context.profile_summary.recovery_preference[:24]}”"
                if context.profile_summary.recovery_preference
                else ""
            )
            return (
                f"如果条件允许，先安排 {plan.action_minutes} 分钟真正脱离任务的休息，"
                f"回来后再只选一件最小可做的事{recovery}。"
            )
        if plan.intervention_type == "micro_break":
            return (
                f"如果方便，可以先留 {min(5, max(2, plan.action_minutes))} 分钟不处理任务，"
                "喝点水、站起来活动一下或看向远处，再进入下一项安排。"
            )
        if plan.intervention_type == "generic_fallback":
            return "如果方便，可以先用几分钟补水、活动一下，再确认下一件最小可做的事。"
        preference = (
            f"也可以采用你记录的恢复方式“{context.profile_summary.recovery_preference[:24]}”。"
            if context.profile_summary.recovery_preference
            else ""
        )
        return (
            f"可以用 {plan.action_minutes} 分钟检查接下来的优先级，补水或活动一下，"
            f"只保留眼前最需要做的一件事。{preference}"
        )
