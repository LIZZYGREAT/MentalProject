"""Context-aware, rate-limit-independent progress copy selection."""

from __future__ import annotations

import hashlib
from typing import Protocol

from app.presentation.contracts import AgentActivityEvent


TOOL_STAGE = {
    "calendar_connection_status": "calendar",
    "calendar_list_calendars": "calendar",
    "calendar_list_events": "calendar",
    "care_get_today_context": "context",
    "care_get_recent_state": "context",
    "care_run_today_assessment": "assessment",
    "care_get_pressure_curve": "forecast",
    "care_get_checkin_card": "card",
    "care_record_checkin": "record_checkin",
    "care_get_support": "support",
    "care_update_preferences": "preferences",
    "care_respond_to_latest_intervention": "intervention_feedback",
    "calendar_create_event": "calendar_mutation",
    "calendar_update_event": "calendar_mutation",
    "calendar_delete_event": "calendar_mutation",
}

_GENERIC_PROGRESS_MESSAGES = (
    "我正在整理这次结果，稍等一下。",
    "我在核对相关信息，马上整理好。",
    "我正在处理这项任务，结果很快发你。",
    "我在把相关内容汇总起来，请稍等。",
)


class ProgressPresentationState(Protocol):
    sent: int
    last_stage: str | None
    sent_keys: set[str]


class ProgressPresenter:
    def present(
        self, event: AgentActivityEvent, *, state: ProgressPresentationState
    ) -> str | None:
        tool_name = str(event.tool_name or "")
        stage = TOOL_STAGE.get(tool_name)
        if stage is None:
            return None

        previous_stage = state.last_stage
        if event.kind == "tool_started":
            state.last_stage = stage
            if stage == "calendar":
                return "我先看看相关日程。"
            if stage == "context":
                return "我先看看你今天已记录的状态。"
            if stage == "assessment":
                if previous_stage == "calendar":
                    return "日程信息拿到了，我正在结合这些安排计算压力变化。"
                return "我正在结合今天的信息进行评估。"
            if stage == "forecast":
                if previous_stage == "calendar":
                    return "日程信息拿到了，我正在结合这些安排计算压力变化。"
                return "我正在结合今天的状态和日程计算压力趋势。"
            if stage == "card":
                if previous_stage == "forecast":
                    return "压力趋势已经算好了，我在整理成更直观的结果。"
                return "我在准备这次状态记录。"
            if stage == "record_checkin":
                return "我在记录这次状态。"
            if stage == "support":
                return "我在结合当前状态整理更合适的支持建议。"
            if stage == "preferences":
                return "我正在更新你的提醒与关怀设置。"
            if stage == "intervention_feedback":
                return "我正在记录这次反馈。"
            if tool_name == "calendar_create_event":
                return "我正在核对时间并创建这条日程。"
            if tool_name == "calendar_update_event":
                return "我正在修改这条日程。"
            if tool_name == "calendar_delete_event":
                return "我正在处理这条已确认的删除操作。"
        return None

    def delayed(
        self, user_text: str = "", *, state: ProgressPresentationState
    ) -> str | None:
        if state.sent:
            return None
        digest = hashlib.sha256(str(user_text).encode("utf-8")).digest()
        return _GENERIC_PROGRESS_MESSAGES[digest[0] % len(_GENERIC_PROGRESS_MESSAGES)]

    def key_for(
        self, event: AgentActivityEvent, *, state: ProgressPresentationState
    ) -> str:
        stage = TOOL_STAGE.get(str(event.tool_name or ""), "generic")
        return f"{event.kind}:{stage}"
