"""Shared deterministic care domain service for Web routes and bot tools."""

from __future__ import annotations

from datetime import date
import os
import re
from typing import Any, Dict, Optional

from auth.database import AppDatabase
from services.onboarding import new_id, utc_now
from services.prediction_service import PredictionService
from settings.model_defaults import BASE_DATA_DIR
from utils.get_token import FeishuAPI


TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
ALLOWED_TONES = {"brief_warm", "calm_practical", "minimal"}
ALLOWED_SUPPORT = {
    "task_breakdown",
    "short_break",
    "breathing",
    "quiet_companionship",
}


class CareService:
    def __init__(
        self,
        database: AppDatabase,
        *,
        prediction_service: Optional[PredictionService] = None,
        token_path_factory=None,
    ):
        self.database = database
        self.prediction_service = prediction_service or PredictionService(database)
        self.token_path_factory = token_path_factory

    def get_today_context(self, user_id: int, local_date: Optional[str] = None) -> Dict[str, Any]:
        local_date = str(local_date or date.today().isoformat())
        run = self.database.latest_prediction_run_for_date(int(user_id), local_date)
        latest_checkin = None
        for item in reversed(
            self.database.list_feedback_observations(
                int(user_id), target_date=local_date, limit=200
            )
        ):
            if item.get("feedback_type") == "momentary_state":
                latest_checkin = item
                break
        if not run:
            return {
                "local_date": local_date,
                "has_prediction": False,
                "latest_checkin": self._minimal_checkin(latest_checkin),
            }
        result = run.get("result") or {}
        return {
            "local_date": local_date,
            "has_prediction": True,
            "prediction_run_id": run["prediction_run_id"],
            "stress_0_10": round(float(result.get("end_S", 0.0)) / 10.0, 1),
            "vitality_0_10": round(float(result.get("end_V", result.get("end_E", 0.0))) / 10.0, 1),
            "alert_count": len(result.get("alerts") or []),
            "latest_checkin": self._minimal_checkin(latest_checkin),
            "created_at": run.get("created_at"),
        }

    def record_checkin(
        self,
        user_id: int,
        payload: Dict[str, Any],
        source: str = "feishu_bot",
    ) -> Dict[str, Any]:
        normalized = self._validate_checkin(payload)
        feedback = {
            "feedback_id": new_id(),
            "schema_version": "feedback_observation.v2",
            "prediction_run_id": None,
            "feedback_type": "momentary_state",
            "target_time": payload.get("target_time") or utc_now(),
            "payload": {**normalized, "source": str(source)},
            "reported_at": utc_now(),
            "retrospective": bool(payload.get("retrospective", False)),
        }
        self.database.save_feedback_observation(int(user_id), feedback)
        return {
            "feedback_id": feedback["feedback_id"],
            "stress_0_10": normalized["stress_0_10"],
            "vitality_0_10": normalized["vitality_0_10"],
            "activity": normalized["activity"],
        }

    def run_today_assessment(
        self,
        user_id: int,
        local_date: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self.prediction_service.run_daily_prediction(
            int(user_id),
            str(local_date or date.today().isoformat()),
        )

    def get_support(
        self,
        user_id: int,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        context = context or self.get_today_context(int(user_id))
        preferences = self.get_preferences(int(user_id))
        support_types = preferences.get("preferred_support") or ["short_break"]
        support_type = support_types[0] if support_types else "short_break"
        suggestions = {
            "task_breakdown": "先选出眼前最小的一步，只做 10 分钟；完成后再决定下一步。",
            "short_break": "如果条件允许，先离开当前任务 5 分钟，喝口水、活动一下，再回来。",
            "breathing": "可以试着放慢呼吸：轻轻吸气 4 秒、呼气 6 秒，重复几轮，以舒适为准。",
            "quiet_companionship": "先不用急着解决所有事情。给自己两分钟安静下来，只确认下一件必须做的事。",
        }
        prefix = ""
        if context.get("has_prediction"):
            stress = float(context.get("stress_0_10", 0.0))
            vitality = float(context.get("vitality_0_10", 0.0))
            if stress >= 7:
                prefix = "今天的模型结果提示负荷偏高。"
            elif vitality <= 4:
                prefix = "今天的模型结果提示活力偏低。"
            else:
                prefix = "今天的状态目前相对平稳。"
        else:
            prefix = "今天还没有运行评估，我先给你一个轻量建议。"
        text = f"{prefix}{suggestions.get(support_type, suggestions['short_break'])}"
        if preferences.get("tone") == "minimal":
            text = suggestions.get(support_type, suggestions["short_break"])
        return {
            "text": text,
            "support_type": support_type,
            "prediction_run_id": context.get("prediction_run_id"),
        }

    def submit_review(
        self,
        user_id: int,
        delivery_id: str,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        delivery = self.database.care_delivery_for_user(int(user_id), str(delivery_id))
        if not delivery:
            raise ValueError("关怀消息不存在或不属于当前用户")
        review = str(payload.get("review") or "").strip()
        if review not in {"helpful", "not_helpful", "remind_later", "mute_today"}:
            raise ValueError("不支持的关怀反馈")
        feedback = {
            "feedback_id": new_id(),
            "schema_version": "feedback_observation.v2",
            "prediction_run_id": delivery.get("prediction_run_id"),
            "feedback_type": "care_review",
            "target_time": delivery.get("sent_at") or delivery.get("created_at"),
            "payload": {"delivery_id": str(delivery_id), "review": review},
            "reported_at": utc_now(),
            "retrospective": False,
        }
        self.database.save_feedback_observation(int(user_id), feedback)
        return {"feedback_id": feedback["feedback_id"], "review": review}

    def get_preferences(self, user_id: int) -> Dict[str, Any]:
        profile = self.database.latest_profile_snapshot(int(user_id)) or {}
        defaults = profile.get("care_preferences") or {}
        return self.database.get_care_preferences(int(user_id), defaults=defaults)

    def update_preferences(self, user_id: int, changes: Dict[str, Any]) -> Dict[str, Any]:
        allowed = {
            "feishu_proactive_enabled",
            "quiet_start",
            "quiet_end",
            "max_daily_messages",
            "tone",
            "preferred_support",
            "allow_personal_history_reference",
            "allow_external_llm",
        }
        unknown = sorted(set(changes) - allowed)
        if unknown:
            raise ValueError(f"不支持的偏好字段: {', '.join(unknown)}")
        normalized = dict(changes)
        for key in ("quiet_start", "quiet_end"):
            if key in normalized and not TIME_PATTERN.fullmatch(str(normalized[key])):
                raise ValueError(f"{key} 必须使用 HH:MM")
        if "max_daily_messages" in normalized:
            value = int(normalized["max_daily_messages"])
            if not 0 <= value <= 10:
                raise ValueError("max_daily_messages 必须在 0–10 之间")
            normalized["max_daily_messages"] = value
        if "tone" in normalized and normalized["tone"] not in ALLOWED_TONES:
            raise ValueError("不支持的提醒语气")
        if "preferred_support" in normalized:
            support = normalized["preferred_support"]
            if isinstance(support, str):
                support = [support]
            if not isinstance(support, list) or not support or any(
                item not in ALLOWED_SUPPORT for item in support
            ):
                raise ValueError("不支持的帮助方式")
            normalized["preferred_support"] = support
        for key in (
            "feishu_proactive_enabled",
            "allow_personal_history_reference",
            "allow_external_llm",
        ):
            if key in normalized and not isinstance(normalized[key], bool):
                raise ValueError(f"{key} 必须是布尔值")
        return self.database.update_care_preferences(int(user_id), normalized)

    def calendar_connection_status(self, user_id: int) -> Dict[str, Any]:
        try:
            api = FeishuAPI(require_secret=False)
            path = (
                self.token_path_factory(int(user_id))
                if self.token_path_factory
                else os.path.join(BASE_DATA_DIR, "user_tokens", f"user_{int(user_id)}.json")
            )
            status = api.get_connection_status(path, refresh=bool(api.app_secret))
            return {
                "connected": bool(status.get("valid")),
                "needs_reauthorization": bool(status.get("needs_reauthorization")),
            }
        except Exception:
            return {"connected": False, "needs_reauthorization": False}

    @staticmethod
    def _validate_checkin(payload: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("打卡内容必须是对象")
        missing = []
        for key in (
            "stress_0_10",
            "vitality_0_10",
            "activity",
            "stress_event_since_last",
            "event_ongoing",
        ):
            if key not in payload or payload.get(key) in (None, ""):
                missing.append(key)
        if missing:
            raise ValueError(f"打卡缺少字段: {', '.join(missing)}")
        result = dict(payload)
        for key in ("stress_0_10", "vitality_0_10"):
            try:
                value = float(result[key])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{key} 必须是数字") from exc
            if not 0 <= value <= 10:
                raise ValueError(f"{key} 必须在 0–10 之间")
            result[key] = value
        for key in ("stress_event_since_last", "event_ongoing"):
            value = result[key]
            if isinstance(value, str) and value.lower() in {"true", "false"}:
                value = value.lower() == "true"
            if not isinstance(value, bool):
                raise ValueError(f"{key} 必须是布尔值")
            result[key] = value
        result["activity"] = str(result["activity"]).strip()[:120]
        if not result["activity"]:
            raise ValueError("activity 不能为空")
        return result

    @staticmethod
    def _minimal_checkin(item: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not item:
            return None
        payload = item.get("payload") or {}
        return {
            "stress_0_10": payload.get("stress_0_10"),
            "vitality_0_10": payload.get("vitality_0_10", payload.get("energy_0_10")),
            "activity": payload.get("activity"),
            "reported_at": item.get("reported_at"),
        }
