"""Consent and safety policy for explicit participant memory."""

from __future__ import annotations

import re
import uuid
from typing import Any


MEMORY_TYPES = frozenset({
    "stable_fact", "goal", "routine", "context", "preferred_name",
})
_FORBIDDEN = re.compile(
    r"(?:忽略|绕过|覆盖|取消).{0,12}(?:系统|安全|权限|授权|规则)|"
    r"(?:token|secret|password|api[_ -]?key|密码|密钥)\s*[:=]",
    re.I,
)
_SENSITIVE_HEALTH_DATA = re.compile(
    r"(?:抑郁症|焦虑症|双相(?:情感)?(?:障碍)?|躁郁症|精神分裂|精神疾病|"
    r"自伤风险|自杀风险|自杀倾向|自残倾向|临床筛查|筛查结果|"
    r"PHQ-?9|GAD-?7|抗抑郁药|抗焦虑药|精神科用药|心理治疗|药物治疗)|"
    r"(?:我|本人|用户|他|她).{0,12}(?:确诊|诊断为|患有).{0,12}(?:精神|心理|情绪)",
    re.I,
)
_STRUCTURED_PREFERENCE_REQUEST = re.compile(
    r"(?:以后|请|希望你|你要|回答时).{0,20}(?:回答|回复|表达|语气|建议|听我说|"
    r"关心我|跟进|提醒).{0,20}(?:短|简洁|详细|直接|温和|先问|先听|不要|最多|主动)|"
    r"(?:以后|请).{0,8}(?:直接|温和|简短|详细).{0,8}(?:一点|回复|回答)|"
    r"(?:先问我.{0,8}(?:要不要|想不想).{0,6}建议|先听我说完|"
    r"压力大时别一次给很多建议)",
    re.I,
)


def normalize_memory(content: str, memory_type: str) -> tuple[str, str | None]:
    normalized = " ".join(str(content).split()).strip("。 ")
    if not 1 <= len(normalized) <= 500:
        raise ValueError("memory content must contain 1 to 500 characters")
    if memory_type not in MEMORY_TYPES:
        raise ValueError("unsupported memory type")
    if _FORBIDDEN.search(normalized):
        raise ValueError("memory cannot alter system, safety, authorization, or secrets")
    if _SENSITIVE_HEALTH_DATA.search(normalized):
        raise ValueError("sensitive clinical or health data cannot become durable memory")
    if _STRUCTURED_PREFERENCE_REQUEST.search(normalized):
        raise ValueError("interaction or support preferences cannot become durable memory")
    key = None
    if memory_type == "preferred_name" or re.search(r"(?:叫我|称呼我|我的名字|我叫)", normalized):
        key = "preferred_name"
    elif memory_type == "routine" and re.search(r"(?:睡|起床|作息)", normalized):
        key = "sleep_routine"
    return normalized.casefold(), key


class MemoryService:
    def __init__(self, repository: Any) -> None:
        self.repository = repository

    def remember_explicit(self, participant_id: uuid.UUID, *, memory_type: str, content: str) -> dict:
        normalized, conflict_key = normalize_memory(content, memory_type)
        return self.repository.remember(
            participant_id, memory_type=memory_type,
            content=" ".join(str(content).split())[:500],
            normalized_content=normalized, conflict_key=conflict_key,
            source="user_explicit", consent_basis="user_requested_memory",
            confidence=1.0,
        )

    def list(self, participant_id: uuid.UUID) -> list[dict]:
        return self.repository.list_active(participant_id)

    def delete(self, participant_id: uuid.UUID, memory_id: uuid.UUID) -> bool:
        return self.repository.delete(participant_id, memory_id)

    def replace(self, participant_id: uuid.UUID, memory_id: uuid.UUID, *, content: str) -> dict | None:
        active = next(
            (row for row in self.repository.list_active(participant_id) if row["id"] == str(memory_id)),
            None,
        )
        if active is None:
            return None
        normalized, conflict_key = normalize_memory(content, active["memory_type"])
        return self.repository.replace(
            participant_id, memory_id, content=" ".join(str(content).split())[:500],
            normalized_content=normalized, conflict_key=conflict_key,
        )

    def clear_all(self, participant_id: uuid.UUID) -> int:
        return self.repository.clear_all(participant_id)

    def retrieve(self, participant_id: uuid.UUID, query: str) -> list[dict]:
        return self.repository.retrieve(participant_id, query=query, limit=5, max_chars=1200)
