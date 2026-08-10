"""Deterministic pre/post checks reused from the reviewed care safety behavior."""

from __future__ import annotations

import re


HIGH_RISK_PATTERN = re.compile(
    r"(不想活|想死|自杀|结束生命|伤害自己|自残|活着没意思)", re.IGNORECASE
)
DIAGNOSTIC_PATTERN = re.compile(r"(你(已经|就是|一定|肯定)?(患有|得了|确诊)|临床诊断|医学诊断|确诊为)")
FIXED_HIGH_RISK_RESPONSE = (
    "我很重视你刚才说的话。这个机器人不能替代紧急服务或专业支持。"
    "如果你现在可能伤害自己或处于立即危险中，请马上联系当地急救服务，"
    "并尽快联系一位你信任、能陪在你身边的人；也请考虑联系学校心理中心或专业人员。"
)


class SafetyService:
    def precheck(self, user_text: str, *, chat_type: str = "p2p") -> str | None:
        if str(chat_type).lower() not in {"p2p", "private", "single"}:
            return "为了保护隐私，个人状态、日历和历史反馈只在机器人单聊中提供。"
        if HIGH_RISK_PATTERN.search(str(user_text or "")):
            return FIXED_HIGH_RISK_RESPONSE
        return None

    def postcheck(self, text: str) -> str:
        value = str(text or "").strip()
        if not value:
            return "我暂时无法生成这条建议。你可以稍后重试。"
        if DIAGNOSTIC_PATTERN.search(value):
            return "当前信息只用于日常状态参考，不能作为医学诊断。如有需要，请联系专业支持。"
        return value[:4000]
