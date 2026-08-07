"""Process one claimed Feishu event with trusted identity and deterministic tools."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, Optional
from urllib.parse import urlsplit, urlunsplit

from auth.database import AppDatabase
from integrations.feishu import cards
from integrations.feishu.identity import FeishuIdentityService
from services.care_agent import CareIntent, DeterministicCareRouter
from services.care_delivery import CareDeliveryPolicy
from services.care_safety import CareSafetyService
from services.care_service import CareService
from services.care_tools import CareToolbox
from services.prediction_service import OnboardingRequiredError, PredictionServiceError


@dataclass
class OutboundMessage:
    chat_id: str
    msg_type: str
    content: Dict[str, Any]
    delivery_id: Optional[str] = None
    ignored: bool = False


class FeishuMessageProcessor:
    """Keep authorization, routing, tools, and safety separate from SDK callbacks."""

    def __init__(
        self,
        database: AppDatabase,
        identity_service: FeishuIdentityService,
        care_service: CareService,
        *,
        web_base_url: str,
        private_chat_only: bool = True,
    ):
        self.database = database
        self.identity_service = identity_service
        self.care_service = care_service
        self.web_base_url = str(web_base_url or "").strip()
        self.private_chat_only = bool(private_chat_only)
        self.router = DeterministicCareRouter()
        self.safety = CareSafetyService()
        self.deliveries = CareDeliveryPolicy(database)

    def process(self, event: Dict[str, Any]) -> OutboundMessage:
        chat_id = str(event["chat_id"])
        chat_type = str(event.get("chat_type") or "p2p").lower()
        text = str((event.get("content") or {}).get("text") or "")
        if text and self.safety.has_high_risk_expression(text):
            return self._text(
                chat_id,
                self.safety.review("", source_user_text=text),
            )
        if self.private_chat_only and chat_type not in {"p2p", "private", "single"}:
            return self._text(
                chat_id,
                self.safety.review(
                    "为了保护隐私，个人关怀只在机器人单聊中提供。",
                    chat_type=chat_type,
                    contains_personal_context=True,
                ),
            )
        binding = self.identity_service.resolve_binding(
            str(event.get("tenant_key") or ""),
            str(event.get("sender_open_id") or ""),
        )
        if not binding:
            token = self.identity_service.create_binding_token(
                tenant_key=str(event.get("tenant_key") or ""),
                open_id=str(event.get("sender_open_id") or ""),
                chat_id=chat_id,
            )
            return OutboundMessage(
                chat_id=chat_id,
                msg_type="interactive",
                content=cards.binding_card(token["bind_url"], token["expires_at"]),
            )
        if not binding.get("user_is_active"):
            return self._text(chat_id, "当前项目账号已停用，请联系管理员。")

        toolbox = CareToolbox(self.care_service, int(binding["user_id"]))
        content = event.get("content") or {}
        if event.get("event_type") == "card_action":
            action = dict(content.get("action") or {})
            action.setdefault("name", content.get("action_name"))
            intent = self.router.route_action(action)
        elif event.get("message_type") != "text":
            return self._text(chat_id, "暂时只支持文字消息和关怀卡片。你可以发送“帮助”查看可用功能。")
        else:
            intent = self.router.route_text(text)
        try:
            return self._execute_intent(
                event,
                int(binding["user_id"]),
                toolbox,
                intent,
            )
        except ValueError as exc:
            return self._text(
                chat_id,
                f"这次输入未保存：{str(exc)}。请检查后重新提交。",
            )

    def _execute_intent(
        self,
        event: Dict[str, Any],
        user_id: int,
        toolbox: CareToolbox,
        intent: CareIntent,
    ) -> OutboundMessage:
        chat_id = str(event["chat_id"])
        if intent.name == "open_checkin":
            return OutboundMessage(chat_id, "interactive", cards.checkin_card())
        if intent.name == "record_checkin":
            payload = dict(intent.arguments.get("payload") or {})
            payload.pop("action", None)
            payload.pop("name", None)
            result = toolbox.execute("care_record_checkin", payload=payload)
            return self._text(
                chat_id,
                f"已记录：压力 {result['stress_0_10']:g}/10，活力 {result['vitality_0_10']:g}/10。谢谢你停下来留意自己。",
            )
        if intent.name == "get_today":
            result = toolbox.execute("care_get_today_context")
            if not result["has_prediction"]:
                latest = result.get("latest_checkin")
                note = ""
                if latest:
                    note = (
                        f" 最近一次打卡为压力 {latest.get('stress_0_10')}/10、"
                        f"活力 {latest.get('vitality_0_10')}/10。"
                    )
                return self._text(chat_id, f"今天还没有运行状态评估。{note}发送“运行今日评估”即可开始。")
            return self._text(
                chat_id,
                (
                    f"今天的最新结果：压力约 {result['stress_0_10']}/10，"
                    f"活力约 {result['vitality_0_10']}/10。"
                    "这是日常状态参考，不是医学诊断。"
                ),
                personal=True,
            )
        if intent.name == "run_assessment":
            try:
                result = toolbox.execute("care_run_today_assessment")
            except OnboardingRequiredError as exc:
                return self._text(chat_id, str(exc))
            except PredictionServiceError:
                return self._text(chat_id, "今日评估暂时无法完成，请稍后重试。")
            summary = result["result"]
            degraded = " 本次未读取到日历，已基于现有画像和打卡降级完成。" if result["calendar_degraded"] else ""
            return self._text(
                chat_id,
                (
                    f"今日评估已完成：压力约 {float(summary['end_S']) / 10:.1f}/10，"
                    f"活力约 {float(summary['end_V']) / 10:.1f}/10。{degraded}"
                    "发送“给我一点支持”可查看建议。"
                ),
                personal=True,
            )
        if intent.name == "get_support":
            support = toolbox.execute("care_get_support")
            delivery = self.deliveries.create_requested_delivery(
                user_id=user_id,
                request_event_id=str(event["event_id"]),
                prediction_run_id=support.get("prediction_run_id"),
                local_date=date.today().isoformat(),
            )
            safe_text = self.safety.review(
                support["text"],
                chat_type=str(event.get("chat_type") or "p2p"),
                contains_personal_context=True,
            )
            return OutboundMessage(
                chat_id,
                "interactive",
                cards.feedback_card(safe_text, delivery["delivery_id"]),
                delivery_id=delivery["delivery_id"],
            )
        if intent.name == "submit_review":
            result = toolbox.execute(
                "care_submit_review",
                delivery_id=intent.arguments.get("delivery_id"),
                payload=intent.arguments.get("payload") or {},
            )
            messages = {
                "helpful": "收到，谢谢你的反馈。",
                "not_helpful": "收到，我会把这次标记为帮助不大。",
                "remind_later": "收到。主动定时提醒尚未启用，本次反馈已记录。",
                "mute_today": "收到，今天不再基于这条关怀继续提醒。",
            }
            return self._text(chat_id, messages[result["review"]])
        if intent.name == "calendar_status":
            status = toolbox.execute("calendar_connection_status")
            if status["connected"]:
                return self._text(chat_id, "你的个人飞书日历已连接。后续评估会继续使用你自己的授权凭证。")
            return OutboundMessage(
                chat_id,
                "interactive",
                cards.calendar_connection_card(self._web_url()),
            )
        if intent.name == "update_preferences":
            result = toolbox.execute(
                "care_update_preferences",
                changes=intent.arguments["changes"],
            )
            state = "开启" if result["feishu_proactive_enabled"] else "关闭"
            return self._text(chat_id, f"已{state}主动关怀偏好。主动定时推送将在后续阶段启用。")
        if intent.name == "revoke_help":
            return self._text(chat_id, "为避免误操作，请登录 Web 设置页解除飞书机器人绑定。")
        return OutboundMessage(chat_id, "interactive", cards.help_card())

    def _web_url(self) -> str:
        parts = urlsplit(self.web_base_url)
        return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", "", ""))

    def _text(self, chat_id: str, text: str, *, personal: bool = False) -> OutboundMessage:
        return OutboundMessage(
            chat_id,
            "text",
            {
                "text": self.safety.review(
                    text,
                    chat_type="p2p",
                    contains_personal_context=personal,
                )
            },
        )
