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
    ACTIVE_REQUEST_RECOVERY_GRACE = timedelta(minutes=5)
    RECOVERABLE = {
        "prepared",
        "remote_outcome_unknown",
        "remote_committed",
        "fencing_failed",
        "fenced",
        "pending",  # Backward compatibility for rows written before the saga.
    }

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
        operation: Mapping[str, Any] | None = None,
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
            work_json={"operation": dict(operation or {}), "targets": targets},
            status="prepared",
            attempt_count=0,
            # Do not let the background recovery scan race the in-flight
            # provider request that this pre-intent protects. A committed
            # remote result becomes immediately due in mark_remote_committed.
            next_attempt_at=created_at + self.ACTIVE_REQUEST_RECOVERY_GRACE,
            created_at=created_at,
            updated_at=created_at,
        )
        with self.database.session() as session:
            session.add(row)
            session.flush()
            return self._view(row)

    def mark_remote_committed(
        self,
        reconciliation_id: uuid.UUID | str,
        *,
        provider_result: Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> bool:
        changed_at = _aware(now or datetime.now(timezone.utc))
        with self.database.session() as session:
            row = session.get(
                CalendarMutationReconciliation,
                uuid.UUID(str(reconciliation_id)),
                with_for_update=True,
            )
            if row is None or row.status in {"remote_failed", "resolved"}:
                return False
            work = dict(row.work_json or {})
            work["provider_result"] = dict(provider_result or {})
            row.work_json = work
            row.status = "remote_committed"
            # Once the provider has committed, recovery must be able to take
            # over immediately if the request coroutine is cancelled before
            # local fail-close. A short DB claim, not a time grace, prevents
            # duplicate processing with the live request path.
            row.next_attempt_at = (
                datetime.fromisoformat(str(work["processing_claim_until"]))
                if work.get("processing_claim_until")
                else changed_at
            )
            row.last_error_class = None
            row.updated_at = changed_at
            return True

    def claim_processing(
        self,
        reconciliation_id: uuid.UUID | str,
        *,
        claim_token: uuid.UUID | str,
        lease_seconds: int = 60,
        force: bool = False,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        """Atomically lease fencing/reconciliation work across processes."""

        changed_at = _aware(now or datetime.now(timezone.utc))
        token = str(claim_token)
        with self.database.session() as session:
            row = session.get(
                CalendarMutationReconciliation,
                uuid.UUID(str(reconciliation_id)),
                with_for_update=True,
            )
            if row is None or row.status in {"remote_failed", "resolved"}:
                return None
            work = dict(row.work_json or {})
            current_token = str(work.get("processing_claim_token") or "")
            raw_until = work.get("processing_claim_until")
            try:
                current_until = _aware(datetime.fromisoformat(str(raw_until)))
            except (TypeError, ValueError):
                current_until = None
            if (
                not force
                and current_token
                and current_token != token
                and current_until
                and current_until > changed_at
            ):
                return None
            lease_until = changed_at + timedelta(seconds=max(5, int(lease_seconds)))
            work["processing_claim_token"] = token
            work["processing_claim_until"] = lease_until.isoformat()
            row.work_json = work
            row.next_attempt_at = lease_until
            row.updated_at = changed_at
            session.flush()
            return self._view(row)

    def release_processing(
        self,
        reconciliation_id: uuid.UUID | str,
        *,
        claim_token: uuid.UUID | str,
        now: datetime | None = None,
    ) -> bool:
        """Release a processing lease and make unfinished durable work due now."""

        changed_at = _aware(now or datetime.now(timezone.utc))
        token = str(claim_token)
        with self.database.session() as session:
            row = session.get(
                CalendarMutationReconciliation,
                uuid.UUID(str(reconciliation_id)),
                with_for_update=True,
            )
            if row is None or row.status in {"remote_failed", "resolved"}:
                return False
            work = dict(row.work_json or {})
            if str(work.get("processing_claim_token") or "") != token:
                return False
            work.pop("processing_claim_token", None)
            work.pop("processing_claim_until", None)
            row.work_json = work
            row.next_attempt_at = changed_at
            row.updated_at = changed_at
            return True

    def mark_remote_failed(
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
            row.status = "remote_failed"
            row.next_attempt_at = None
            row.last_error_class = str(error_class)[:128]
            row.resolved_at = changed_at
            row.updated_at = changed_at
            return True

    def mark_remote_outcome_unknown(
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
            if row is None or row.status in {"remote_failed", "resolved"}:
                return False
            row.status = "remote_outcome_unknown"
            row.next_attempt_at = changed_at
            row.last_error_class = str(error_class)[:128]
            row.resolved_at = None
            row.updated_at = changed_at
            return True

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

    def recoverable_before(
        self, process_started_at: datetime, *, limit: int = 500
    ) -> list[dict[str, Any]]:
        """Return old-process work regardless of its live-request grace."""

        cutoff = _aware(process_started_at)
        with self.database.session() as session:
            rows = session.execute(
                select(CalendarMutationReconciliation).where(
                    CalendarMutationReconciliation.status.in_(self.RECOVERABLE),
                    CalendarMutationReconciliation.created_at < cutoff,
                ).order_by(CalendarMutationReconciliation.created_at).limit(
                    max(1, min(int(limit), 1000))
                )
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
            outcome_unknown = row.status == "remote_outcome_unknown"
            work = dict(row.work_json or {})
            work["fenced_at"] = changed_at.isoformat()
            row.work_json = work
            if not outcome_unknown:
                row.status = "fenced"
            # An active processing owner also owns the managed refresh. Keep
            # other processes out until it resolves, retries, or releases.
            row.next_attempt_at = (
                datetime.fromisoformat(str(work["processing_claim_until"]))
                if work.get("processing_claim_until")
                else changed_at
            )
            if not outcome_unknown:
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
            work = dict(row.work_json or {})
            work.pop("processing_claim_token", None)
            work.pop("processing_claim_until", None)
            row.work_json = work
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
            work = dict(row.work_json or {})
            work.pop("processing_claim_token", None)
            work.pop("processing_claim_until", None)
            row.work_json = work
            delay = min(3600, 30 * (2 ** max(0, row.attempt_count - 1)))
            row.next_attempt_at = changed_at + timedelta(seconds=delay)
            # A refresh retry after successful fencing must not invalidate the
            # same forecasts again. Earlier failures still need the fence.
            if row.status not in {"fenced", "remote_outcome_unknown"}:
                row.status = "fencing_failed"
            row.last_error_class = str(error_class)[:128]
            row.updated_at = changed_at
            return True
