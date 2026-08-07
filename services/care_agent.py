"""Deterministic intent router; the general LLM agent remains disabled in MVP."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Dict


NUMBER = r"(?:10(?:\.0+)?|[0-9](?:\.\d+)?)"
STRESS_PATTERN = re.compile(rf"压力(?:大概|约|是|为)?\s*[:：]?\s*({NUMBER})")
VITALITY_PATTERN = re.compile(rf"(?:活力|精力|能量)(?:大概|约|是|为)?\s*[:：]?\s*({NUMBER})")
APPRAISAL_PATTERN = re.compile(
    r"(?:我觉得|我认为|对我来说)?\s*([^，。,.]{1,30}?)\s*(很难|困难|太难|讨厌|不喜欢|喜欢|轻松)"
)


@dataclass(frozen=True)
class CareIntent:
    name: str
    arguments: Dict[str, Any] = field(default_factory=dict)


class DeterministicCareRouter:
    """Recognize the bounded MVP intents without sending content to an LLM."""

    def route_text(self, text: str) -> CareIntent:
        value = str(text or "").strip()
        compact = re.sub(r"\s+", "", value.lower())
        appraisal = APPRAISAL_PATTERN.search(value)
        if appraisal:
            topic = appraisal.group(1).strip("我觉得认为对来说是")
            label = appraisal.group(2)
            arguments: Dict[str, Any] = {"topic": topic}
            if label in {"很难", "困难", "太难"}:
                arguments.update({"perceived_difficulty": 0.85, "threat": 0.70})
            elif label in {"讨厌", "不喜欢"}:
                arguments.update({"dislike": 0.90, "threat": 0.62})
            elif label == "喜欢":
                arguments.update({"dislike": 0.08, "control": 0.70})
            elif label == "轻松":
                arguments.update({"perceived_difficulty": 0.20, "control": 0.80})
            return CareIntent("record_event_appraisal", arguments)
        stress = STRESS_PATTERN.search(value)
        vitality = VITALITY_PATTERN.search(value)
        if stress and vitality:
            return CareIntent(
                "record_checkin",
                {
                    "payload": {
                        "stress_0_10": float(stress.group(1)),
                        "vitality_0_10": float(vitality.group(1)),
                        "activity": "飞书文字打卡",
                        "stress_event_since_last": False,
                        "event_ongoing": False,
                    }
                },
            )
        if any(term in compact for term in ("打卡", "记录此刻", "记录状态")):
            return CareIntent("open_checkin")
        if any(term in compact for term in ("运行今日评估", "开始评估", "运行评估", "重新评估")):
            return CareIntent("run_assessment")
        if any(term in compact for term in ("今天状态", "今日状态", "状态怎么样", "今天怎么样")):
            return CareIntent("get_today")
        if any(term in compact for term in ("确认任务", "任务完成", "完成情况", "日程反馈")):
            return CareIntent("get_event_confirmations")
        if any(term in compact for term in ("给我一点支持", "关怀建议", "怎么缓解", "休息建议", "帮助我")):
            return CareIntent("get_support")
        if any(term in compact for term in ("连接日历", "日历状态", "飞书日历")):
            return CareIntent("calendar_status")
        if any(term in compact for term in ("关闭主动关怀", "停止主动关怀", "不要主动提醒")):
            return CareIntent(
                "update_preferences",
                {"changes": {"feishu_proactive_enabled": False}},
            )
        if any(term in compact for term in ("开启主动关怀", "打开主动关怀")):
            return CareIntent(
                "update_preferences",
                {"changes": {"feishu_proactive_enabled": True}},
            )
        if any(term in compact for term in ("解绑", "解除绑定")):
            return CareIntent("revoke_help")
        return CareIntent("help")

    def route_action(self, action: Dict[str, Any]) -> CareIntent:
        name = str(action.get("action") or action.get("name") or "")
        if name == "care_open_checkin":
            return CareIntent("open_checkin")
        if name == "care_checkin_submit":
            return CareIntent("record_checkin", {"payload": action})
        if name == "care_get_today":
            return CareIntent("get_today")
        if name == "care_run_assessment":
            return CareIntent("run_assessment")
        if name == "care_get_support":
            return CareIntent("get_support")
        if name == "care_get_event_confirmations":
            return CareIntent("get_event_confirmations")
        if name == "care_event_outcome":
            return CareIntent(
                "record_event_outcome",
                {
                    "prediction_run_id": action.get("prediction_run_id"),
                    "event_id": action.get("event_id"),
                    "event_name": action.get("event_name"),
                    "outcome_status": action.get("outcome_status"),
                    "observed_at": action.get("observed_at"),
                },
            )
        if name == "care_calendar_status":
            return CareIntent("calendar_status")
        if name == "care_feedback":
            return CareIntent(
                "submit_review",
                {
                    "delivery_id": action.get("delivery_id"),
                    "payload": {"review": action.get("review")},
                },
            )
        return CareIntent("help")
