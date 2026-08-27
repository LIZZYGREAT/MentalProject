"""Durable reconciliation intents for committed remote Calendar mutations."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping
import uuid

from sqlalchemy import or_, select

from app.db import Database
from app.models import CalendarMutationReconciliation


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class CalendarMutationReconciliationRepository:
    RECOVERABLE = {"pending", "fenced"}

    def __init__(self, database: Database):
        self.database = database

    @staticmethod
    def _view(row: CalendarMutationReconciliation) -> dict[str, Any]:
        return {
            "id": str(row.id),
            "participant_id": str(row.participant_id),
            "mutation_kind": row.mutation_kind,
            "work": dict(row.work_json),
            "status": row.status,
            "attempt_count": row.attempt_count,
            "next_attempt_at": (
                row.next_attempt_at.isoformat() if row.next_attempt_at else None
            ),
            "last_error_class": row.last_error_class,
            "created_at": row.created_at.isoformat(),
            "updated_at": row.updated_at.isoformat(),
            "resolved_at": row.resolved_at.isoformat() if row.resolved_at else None,
        }

    def create(
        self,
        participant_id: uuid.UUID,
        *,
        mutation_kind: str,
        direct_dates: set[date],
        refresh_targets: Mapping[date, bool],
        dependency_sources: Mapping[date, date],
        now: datetime | None = None,
    ) -> dict[str, Any]:
        created_at = _aware(now or datetime.now(timezone.utc))
        targets = []
        for target, refresh_calendar in sorted(refresh_targets.items()):
            targets.append(
                {
                    "local_date": target.isoformat(),
                    "refresh_calendar": bool(refresh_calendar),
                    "requires_invalidation": target in direct_dates,
                    "dependency_source": (
                        dependency_sources[target].isoformat()
                        if target in dependency_sources
                        else None
                    ),
                }
            )
        row = CalendarMutationReconciliation(
            participant_id=participant_id,
            mutation_kind=str(mutation_kind)[:64],
            work_json={"targets": targets},
            status="pending",
            attempt_count=0,
            next_attempt_at=created_at,
            created_at=created_at,
            updated_at=created_at,
        )
        with self.database.session() as session:
            session.add(row)
            session.flush()
            return self._view(row)

    def get(self, reconciliation_id: uuid.UUID | str) -> dict[str, Any] | None:
        with self.database.session() as session:
            row = session.get(
                CalendarMutationReconciliation,
                uuid.UUID(str(reconciliation_id)),
            )
            return self._view(row) if row is not None else None

    def due(
        self, now: datetime | None = None, *, limit: int = 100
    ) -> list[dict[str, Any]]:
        due_at = _aware(now or datetime.now(timezone.utc))
        with self.database.session() as session:
            rows = session.execute(
                select(CalendarMutationReconciliation)
                .where(
                    CalendarMutationReconciliation.status.in_(self.RECOVERABLE),
                    or_(
                        CalendarMutationReconciliation.next_attempt_at.is_(None),
                        CalendarMutationReconciliation.next_attempt_at <= due_at,
                    ),
                )
                .order_by(CalendarMutationReconciliation.created_at)
                .limit(max(1, min(int(limit), 500)))
            ).scalars().all()
            return [self._view(row) for row in rows]

    def mark_fenced(
        self, reconciliation_id: uuid.UUID | str, *, now: datetime | None = None
    ) -> bool:
        changed_at = _aware(now or datetime.now(timezone.utc))
        with self.database.session() as session:
            row = session.get(
                CalendarMutationReconciliation,
                uuid.UUID(str(reconciliation_id)),
                with_for_update=True,
            )
            if row is None or row.status == "resolved":
                return False
            row.status = "fenced"
            row.next_attempt_at = changed_at
            row.last_error_class = None
            row.updated_at = changed_at
            return True

    def mark_resolved(
        self, reconciliation_id: uuid.UUID | str, *, now: datetime | None = None
    ) -> bool:
        changed_at = _aware(now or datetime.now(timezone.utc))
        with self.database.session() as session:
            row = session.get(
                CalendarMutationReconciliation,
                uuid.UUID(str(reconciliation_id)),
                with_for_update=True,
            )
            if row is None:
                return False
            row.status = "resolved"
            row.next_attempt_at = None
            row.last_error_class = None
            row.resolved_at = changed_at
            row.updated_at = changed_at
            return True

    def mark_retry(
        self,
        reconciliation_id: uuid.UUID | str,
        *,
        error_class: str,
        now: datetime | None = None,
    ) -> bool:
        changed_at = _aware(now or datetime.now(timezone.utc))
        with self.database.session() as session:
            row = session.get(
                CalendarMutationReconciliation,
                uuid.UUID(str(reconciliation_id)),
                with_for_update=True,
            )
            if row is None or row.status == "resolved":
                return False
            row.attempt_count += 1
            delay = min(3600, 30 * (2 ** max(0, row.attempt_count - 1)))
            row.next_attempt_at = changed_at + timedelta(seconds=delay)
            row.last_error_class = str(error_class)[:128]
            row.updated_at = changed_at
            return True
