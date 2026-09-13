"""Durable explicit memory repository with participant isolation."""

from __future__ import annotations

from datetime import datetime
import re
import uuid

from sqlalchemy import desc, select

from app.db import Database
from app.models import ParticipantMemoryItem, utc_now


_CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
_ASCII_TOKEN = re.compile(r"[a-z0-9]+")
_TYPE_HINTS = {
    "goal": ("目标", "计划", "准备", "完成", "考试", "答辩", "项目"),
    "routine": ("通常", "一般", "每天", "每周", "作息", "睡", "起床"),
    "preferred_name": ("名字", "叫", "称呼"),
}


def _search_parts(value: str) -> tuple[str, set[str], set[str]]:
    text = str(value).casefold()
    compact = "".join(character for character in text if character.isalnum())
    cjk_grams: set[str] = set()
    for run in _CJK_RUN.findall(text):
        for width in (2, 3):
            cjk_grams.update(run[index:index + width] for index in range(len(run) - width + 1))
    ascii_tokens = {token for token in _ASCII_TOKEN.findall(text) if len(token) > 1}
    return compact, cjk_grams, ascii_tokens


def _relevance(query: str, row: dict) -> int:
    query_compact, query_cjk, query_ascii = _search_parts(query)
    memory_compact, memory_cjk, memory_ascii = _search_parts(row["normalized_content"])
    score = 0
    if min(len(query_compact), len(memory_compact)) >= 2 and (
        query_compact in memory_compact or memory_compact in query_compact
    ):
        score += 100
    score += 3 * len(query_cjk & memory_cjk)
    score += 4 * len(query_ascii & memory_ascii)
    if score and any(hint in query for hint in _TYPE_HINTS.get(row["memory_type"], ())):
        score += 1
    return score


class ParticipantMemoryRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def remember(self, participant_id: uuid.UUID, *, memory_type: str, content: str, normalized_content: str, conflict_key: str | None, source: str, consent_basis: str, confidence: float = 1.0) -> dict:
        now = utc_now()
        with self.database.session() as session:
            row = ParticipantMemoryItem(
                participant_id=participant_id, memory_type=memory_type,
                content=content, normalized_content=normalized_content,
                conflict_key=conflict_key, source=source,
                consent_basis=consent_basis, confidence=confidence,
                status="active", created_at=now, updated_at=now,
            )
            session.add(row)
            session.flush()
            if conflict_key:
                old_rows = session.execute(select(ParticipantMemoryItem).where(
                    ParticipantMemoryItem.participant_id == participant_id,
                    ParticipantMemoryItem.status == "active",
                    ParticipantMemoryItem.conflict_key == conflict_key,
                    ParticipantMemoryItem.id != row.id,
                ).with_for_update()).scalars().all()
                for old in old_rows:
                    old.status = "superseded"
                    old.superseded_by = row.id
                    old.updated_at = now
            return self._view(row)

    def list_active(self, participant_id: uuid.UUID, *, limit: int = 50) -> list[dict]:
        with self.database.session() as session:
            rows = session.execute(select(ParticipantMemoryItem).where(
                ParticipantMemoryItem.participant_id == participant_id,
                ParticipantMemoryItem.status == "active",
            ).order_by(desc(ParticipantMemoryItem.updated_at)).limit(max(1, min(limit, 100)))).scalars().all()
            return [self._view(row) for row in rows]

    def delete(self, participant_id: uuid.UUID, memory_id: uuid.UUID) -> bool:
        with self.database.session() as session:
            row = session.execute(select(ParticipantMemoryItem).where(
                ParticipantMemoryItem.id == memory_id,
                ParticipantMemoryItem.participant_id == participant_id,
                ParticipantMemoryItem.status == "active",
            ).with_for_update()).scalar_one_or_none()
            if row is None:
                return False
            row.status = "deleted"
            row.updated_at = utc_now()
            return True

    def clear_all(self, participant_id: uuid.UUID) -> int:
        with self.database.session() as session:
            rows = session.execute(select(ParticipantMemoryItem).where(
                ParticipantMemoryItem.participant_id == participant_id,
                ParticipantMemoryItem.status == "active",
            ).with_for_update()).scalars().all()
            now = utc_now()
            for row in rows:
                row.status = "deleted"
                row.updated_at = now
            return len(rows)

    def retrieve(self, participant_id: uuid.UUID, *, query: str, limit: int = 5, max_chars: int = 1200) -> list[dict]:
        query = str(query).strip()
        if not query or max_chars <= 0:
            return []
        rows = self.list_active(participant_id, limit=100)
        scored = [(_relevance(query, row), row) for row in rows]
        scored = [(score, row) for score, row in scored if score > 0]
        scored.sort(key=lambda item: (
            item[0], item[1]["last_used_at"] or "", item[1]["updated_at"]
        ), reverse=True)
        selected, total = [], 0
        for _, row in scored:
            size = len(row["content"])
            if total + size > max_chars:
                continue
            selected.append(row)
            total += size
            if len(selected) >= max(1, min(limit, 5)):
                break
        self.record_usage(participant_id, [uuid.UUID(row["id"]) for row in selected])
        return selected

    def record_usage(self, participant_id: uuid.UUID, memory_ids: list[uuid.UUID]) -> None:
        if not memory_ids:
            return
        with self.database.session() as session:
            rows = session.execute(select(ParticipantMemoryItem).where(
                ParticipantMemoryItem.participant_id == participant_id,
                ParticipantMemoryItem.id.in_(memory_ids),
                ParticipantMemoryItem.status == "active",
            )).scalars().all()
            now = utc_now()
            for row in rows:
                row.last_used_at = now

    @staticmethod
    def _view(row: ParticipantMemoryItem) -> dict:
        return {
            "id": str(row.id), "memory_type": row.memory_type,
            "content": row.content, "normalized_content": row.normalized_content,
            "conflict_key": row.conflict_key, "source": row.source,
            "consent_basis": row.consent_basis,
            "confidence": float(row.confidence), "status": row.status,
            "superseded_by": str(row.superseded_by) if row.superseded_by else None,
            "created_at": row.created_at.isoformat(), "updated_at": row.updated_at.isoformat(),
            "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
        }
