"""Context-aware safety gate with deterministic backend policy authority."""

from __future__ import annotations

import re
from typing import Literal, Protocol

from app.contracts.agent_input import AgentTurnInput, ensure_agent_turn_input


SafetyClassification = Literal[
    "self_imminent_risk",
    "self_non_imminent_distress",
    "third_party_concern",
    "quoted_or_academic_context",
    "ordinary",
]

RISK_TOPIC_PATTERN = re.compile(
    r"(不想活|想死|去死|自杀|结束生命|伤害自己|自残|活着没意思)", re.IGNORECASE
)
FIRST_PERSON_IMMINENT_PATTERN = re.compile(
    r"(?:我|本人|自己).{0,18}(?:现在|马上|今晚|今天|已经|正在|想|要|准备|打算|计划)"
    r".{0,12}(?:想死|去死|自杀|结束生命|伤害自己|自残)"
    r"|(?:我|本人).{0,8}(?:不想活了?|活着没意思)",
    re.IGNORECASE,
)
FIRST_PERSON_DISTRESS_PATTERN = re.compile(
    r"(?:我|本人|自己).{0,24}(?:不想活|想死|撑不住|活着没意思|伤害自己|自残)",
    re.IGNORECASE,
)
THIRD_PARTY_CONTEXT_PATTERN = re.compile(
    r"(?:朋友|同学|室友|家人|亲人|孩子|学生|来访者|患者|他|她|他们|别人|第三方)"
    r".{0,30}(?:不想活|想死|自杀|结束生命|伤害自己|自残)",
    re.IGNORECASE,
)
QUOTED_OR_ACADEMIC_PATTERN = re.compile(
    r"(?:论文|研究|文献|新闻|报道|文章|影视|电影|小说|案例|数据|统计|预防|"
    r"引用|原文|台词|标题|摘要|总结|概括|解读).{0,40}"
    r"(?:不想活|想死|自杀|自残|自杀率|自杀预防)"
    r"|(?:不想活|想死|自杀|自残|自杀率|自杀预防).{0,40}"
    r"(?:论文|研究|文献|新闻|报道|文章|影视|电影|小说|案例|数据|统计|预防|"
    r"引用|原文|台词|标题|摘要|总结|概括|解读)",
    re.IGNORECASE,
)
DIAGNOSTIC_PATTERN = re.compile(
    r"(你(已经|就是|一定|肯定)?(患有|得了|确诊)|临床诊断|医学诊断|确诊为)"
)
FIXED_HIGH_RISK_RESPONSE = (
    "我很重视你刚才说的话。这个机器人不能替代紧急服务或专业支持。"
    "如果你现在可能伤害自己或处于立即危险中，请马上联系当地急救服务，"
    "并尽快联系一位你信任、能陪在你身边的人；也请考虑联系学校心理中心或专业人员。"
)


class SafetyClassifier(Protocol):
    def classify(self, text: str) -> SafetyClassification: ...


class SafetySemanticGate:
    """Dedicated context classifier; it cannot override the fixed policy."""

    def classify(self, text: str) -> SafetyClassification:
        value = " ".join(str(text or "").split())
        if not RISK_TOPIC_PATTERN.search(value):
            return "ordinary"
        academic = bool(QUOTED_OR_ACADEMIC_PATTERN.search(value))
        imminent = bool(FIRST_PERSON_IMMINENT_PATTERN.search(value))
        if imminent and not academic:
            return "self_imminent_risk"
        if THIRD_PARTY_CONTEXT_PATTERN.search(value) and not imminent:
            return "third_party_concern"
        if academic:
            return "quoted_or_academic_context"
        if FIRST_PERSON_DISTRESS_PATTERN.search(value):
            return "self_non_imminent_distress"
        # No external semantic service is required. An unclassified risk-topic
        # mention stays on the conservative fixed-policy path.
        return "self_non_imminent_distress"


class SafetyService:
    def __init__(self, semantic_gate: SafetyClassifier | None = None) -> None:
        self.semantic_gate = semantic_gate or SafetySemanticGate()

    def _classification(self, text: str) -> SafetyClassification:
        try:
            return self.semantic_gate.classify(text)
        except Exception:
            return (
                "self_non_imminent_distress"
                if RISK_TOPIC_PATTERN.search(str(text or ""))
                else "ordinary"
            )

    def precheck(self, user_text: str, *, chat_type: str = "p2p") -> str | None:
        if str(chat_type).lower() not in {"p2p", "private", "single"}:
            return "为了保护隐私，个人状态、日历和历史反馈只在机器人单聊中提供。"
        classification = self._classification(str(user_text or ""))
        if classification in {
            "self_imminent_risk", "self_non_imminent_distress"
        }:
            return FIXED_HIGH_RISK_RESPONSE
        return None

    def precheck_turn(
        self,
        turn_input: AgentTurnInput | str,
        *,
        chat_type: str = "p2p",
    ) -> str | None:
        turn = ensure_agent_turn_input(turn_input)
        if str(chat_type).lower() not in {"p2p", "private", "single"}:
            return "为了保护隐私，个人状态、日历和历史反馈只在机器人单聊中提供。"
        combined = str(turn.text or "")
        if turn.trusted_image_context is not None:
            combined = f"{combined}\n{dict(turn.trusted_image_context)}"
        classification = self._classification(combined)
        if classification in {
            "self_imminent_risk", "self_non_imminent_distress"
        }:
            return FIXED_HIGH_RISK_RESPONSE
        return None

    def postcheck(self, text: str) -> str:
        value = str(text or "").strip()
        if not value:
            return "我暂时无法生成这条建议。你可以稍后重试。"
        if DIAGNOSTIC_PATTERN.search(value):
            return "当前信息只用于日常状态参考，不能作为医学诊断。如有需要，请联系专业支持。"
        return value[:4000]
