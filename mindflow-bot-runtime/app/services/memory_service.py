"""Consent and safety policy for explicit participant memory."""

from __future__ import annotations

import re
import uuid
from typing import Any


MEMORY_TYPES = frozenset({
    "preference", "stable_fact", "goal", "routine", "support_preference", "context",
})
_FORBIDDEN = re.compile(
    r"(?:忽略|绕过|覆盖|取消).{0,12}(?:系统|安全|权限|授权|规则)|"
    r"(?:token|secret|password|api[_ -]?key|密码|密钥)\s*[:=]",
    re.I,
)
_CLINICAL_INFERENCE = re.compile(
    r"(?:用户|他|她).{0,8}(?:患有|诊断|抑郁症|焦虑症|精神疾病|情绪不稳定)", re.I
)


def normalize_memory(content: str, memory_type: str) -> tuple[str, str | None]:
    normalized = " ".join(str(content).split()).strip("。 ")
    if not 1 <= len(normalized) <= 500:
        raise ValueError("memory content must contain 1 to 500 characters")
    if memory_type not in MEMORY_TYPES:
        raise ValueError("unsupported memory type")
    if _FORBIDDEN.search(normalized):
        raise ValueError("memory cannot alter system, safety, authorization, or secrets")
    if _CLINICAL_INFERENCE.search(normalized):
        raise ValueError("clinical or psychological inference cannot become durable memory")
    key = None
    if re.search(r"(?:叫我|称呼我|我的名字|我叫)", normalized):
        key = "preferred_name"
    elif memory_type in {"preference", "support_preference"} and re.search(r"(?:简短|简洁|详细|展开)", normalized):
        key = "response_verbosity"
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

    def clear_all(self, participant_id: uuid.UUID) -> int:
        return self.repository.clear_all(participant_id)

    def retrieve(self, participant_id: uuid.UUID, query: str) -> list[dict]:
        return self.repository.retrieve(participant_id, query=query, limit=5, max_chars=1200)
