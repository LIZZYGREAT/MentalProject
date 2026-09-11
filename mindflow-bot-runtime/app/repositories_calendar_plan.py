"""Crash-safe participant-bound batch Calendar mutation plans."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
import uuid

from sqlalchemy import and_, exists, or_, select

from app.db import Database
from app.models import CalendarMutationPlan, CalendarMutationPlanItem


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class CalendarMutationPlanRepository:
    _RETRY_BACKOFF_SECONDS = (5, 15, 60, 300, 900)
    _MAX_OUTCOME_UNKNOWN_ATTEMPTS = len(_RETRY_BACKOFF_SECONDS)
    _MAX_COMPLETION_PRESENTATION_ATTEMPTS = len(_RETRY_BACKOFF_SECONDS)

    @classmethod
    def _next_retry_at(cls, now: datetime, attempt_count: int) -> datetime:
        index = max(0, min(int(attempt_count) - 1, len(cls._RETRY_BACKOFF_SECONDS) - 1))
        return now + timedelta(seconds=cls._RETRY_BACKOFF_SECONDS[index])

    @classmethod
    def _completion_presentation_due(
        cls, updated_at: datetime, attempts: int, now: datetime
    ) -> bool:
        attempts = int(attempts or 0)
        if attempts <= 0:
            return True
        index = max(0, min(attempts - 1, len(cls._RETRY_BACKOFF_SECONDS) - 1))
        return (
            _aware(updated_at) + timedelta(seconds=cls._RETRY_BACKOFF_SECONDS[index])
            <= now
        )

    def __init__(self, database: Database):
        self.database = database

    @staticmethod
    def _item_view(row: CalendarMutationPlanItem) -> dict[str, Any]:
        return {
            "id": str(row.id),
            "plan_id": str(row.plan_id),
            "item_index": int(row.item_index),
            "operation": row.operation,
            "payload": dict(row.payload_json or {}),
            "status": row.status,
            "provider_event_id": row.provider_event_id,
            "error_code": row.error_code,
            "error_detail": row.error_detail,
            "attempt_count": int(row.attempt_count or 0),
            "last_attempt_at": (
                _aware(row.last_attempt_at).isoformat()
                if row.last_attempt_at
                else None
            ),
            "completed_at": (
                _aware(row.completed_at).isoformat() if row.completed_at else None
            ),
            "source_identity": row.source_identity,
            "next_retry_at": (
                _aware(row.next_retry_at).isoformat() if row.next_retry_at else None
            ),
        }

    @classmethod
    def _view(
        cls,
        row: CalendarMutationPlan,
        items: list[CalendarMutationPlanItem] | None = None,
    ) -> dict[str, Any]:
        value = {
            "id": str(row.id),
            "participant_id": str(row.participant_id),
            "operation": row.operation,
            # Confirmation rendering can use this immutable aggregate copy.
            # Durable execution always reads the item ledger below.
            "items": [dict(item) for item in list(row.items_json or [])],
            "status": row.status,
            "result": dict(row.result_json or {}),
            "expires_at": _aware(row.expires_at).isoformat(),
            "created_at": _aware(row.created_at).isoformat(),
            "updated_at": _aware(row.updated_at).isoformat(),
            "completed_at": (
                _aware(row.completed_at).isoformat() if row.completed_at else None
            ),
            "run_requested_at": (
                _aware(row.run_requested_at).isoformat()
                if row.run_requested_at
                else None
            ),
            "lease_owner": row.lease_owner,
            "lease_expires_at": (
                _aware(row.lease_expires_at).isoformat()
                if row.lease_expires_at
                else None
            ),
            "last_progress_at": (
                _aware(row.last_progress_at).isoformat()
                if row.last_progress_at
                else None
            ),
            "status_card_message_id": row.status_card_message_id,
            "status_card_chat_id": row.status_card_chat_id,
            "completion_presented_at": (
                _aware(row.completion_presented_at).isoformat()
                if row.completion_presented_at
                else None
            ),
            "completion_presentation_error": row.completion_presentation_error,
            "completion_presentation_attempts": int(
                row.completion_presentation_attempts or 0
            ),
        }
        if items is not None:
            value["ledger_items"] = [cls._item_view(item) for item in items]
        return value

    @staticmethod
    def _items(session: Any, plan_id: uuid.UUID) -> list[CalendarMutationPlanItem]:
        return list(
            session.scalars(
                select(CalendarMutationPlanItem)
                .where(CalendarMutationPlanItem.plan_id == plan_id)
                .order_by(CalendarMutationPlanItem.item_index)
            )
        )

    def create(
        self,
        participant_id: uuid.UUID,
        *,
        operation: str,
        items: list[dict[str, Any]],
        ttl_minutes: int = 15,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        normalized_operation = str(operation).strip().lower()
        if normalized_operation not in {"create", "delete"}:
            raise ValueError("unsupported calendar mutation plan operation")
        if not 2 <= len(items) <= 20:
            raise ValueError("calendar mutation plan requires 2 to 20 items")
        created_at = _aware(now or datetime.now(timezone.utc))
        row = CalendarMutationPlan(
            participant_id=participant_id,
            operation=normalized_operation,
            items_json=[dict(item) for item in items],
            status="awaiting_confirmation",
            expires_at=created_at + timedelta(minutes=max(1, int(ttl_minutes))),
            created_at=created_at,
            updated_at=created_at,
        )
        with self.database.session() as session:
            session.add(row)
            session.flush()
            ledger = []
            for index, item in enumerate(items):
                entry = CalendarMutationPlanItem(
                    plan_id=row.id,
                    item_index=index,
                    operation=normalized_operation,
                    payload_json=dict(item),
                    status="pending",
                    attempt_count=0,
                    source_identity=f"calendar-plan:{row.id}:{index}",
                    created_at=created_at,
                    updated_at=created_at,
                )
                session.add(entry)
                ledger.append(entry)
            session.flush()
            return self._view(row, ledger)

    def request_execution(
        self,
        participant_id: uuid.UUID,
        plan_id: uuid.UUID | str,
        *,
        status_card_message_id: str | None = None,
        status_card_chat_id: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        requested_at = _aware(now or datetime.now(timezone.utc))
        with self.database.session() as session:
            row = session.get(
                CalendarMutationPlan, uuid.UUID(str(plan_id)), with_for_update=True
            )
            if row is None or str(row.participant_id) != str(participant_id):
                return None
            previous_status = row.status
            if row.status == "awaiting_confirmation" and _aware(row.expires_at) <= requested_at:
                row.status = "expired"
                row.updated_at = requested_at
                row.completed_at = requested_at
            elif row.status == "awaiting_confirmation":
                row.status = "queued"
                row.run_requested_at = requested_at
                row.last_progress_at = requested_at
                row.updated_at = requested_at
                row.status_card_message_id = (
                    str(status_card_message_id)[:256]
                    if status_card_message_id
                    else None
                )
                row.status_card_chat_id = (
                    str(status_card_chat_id)[:256] if status_card_chat_id else None
                )
            session.flush()
            value = self._view(row, self._items(session, row.id))
            value["request_status"] = (
                "queued"
                if previous_status == "awaiting_confirmation" and row.status == "queued"
                else row.status
            )
            value["newly_queued"] = (
                previous_status == "awaiting_confirmation" and row.status == "queued"
            )
            return value

    def claim(
        self,
        participant_id: uuid.UUID,
        plan_id: uuid.UUID | str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        """Compatibility wrapper for integrations predating the runner."""

        value = self.request_execution(participant_id, plan_id, now=now)
        if value is not None:
            value["claim_status"] = (
                "claimed"
                if value.get("newly_queued")
                else value["status"]
            )
        return value

    def cancel(
        self,
        participant_id: uuid.UUID,
        plan_id: uuid.UUID | str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        cancelled_at = _aware(now or datetime.now(timezone.utc))
        with self.database.session() as session:
            row = session.get(
                CalendarMutationPlan, uuid.UUID(str(plan_id)), with_for_update=True
            )
            if row is None or str(row.participant_id) != str(participant_id):
                return None
            if row.status == "awaiting_confirmation":
                row.status = (
                    "expired"
                    if _aware(row.expires_at) <= cancelled_at
                    else "cancelled"
                )
                row.updated_at = cancelled_at
                row.completed_at = cancelled_at
            session.flush()
            return self._view(row, self._items(session, row.id))

    def recover_stale(self, *, now: datetime | None = None) -> int:
        recovered_at = _aware(now or datetime.now(timezone.utc))
        count = 0
        with self.database.session() as session:
            rows = list(
                session.scalars(
                    select(CalendarMutationPlan)
                    .where(
                        CalendarMutationPlan.status == "running",
                        or_(
                            CalendarMutationPlan.lease_expires_at.is_(None),
                            CalendarMutationPlan.lease_expires_at <= recovered_at,
                        ),
                    )
                    .with_for_update(skip_locked=True)
                )
            )
            for row in rows:
                for item in self._items(session, row.id):
                    if item.status in {"creating", "deleting"}:
                        item.status = "outcome_unknown"
                        item.error_code = "runner_interrupted"
                        item.next_retry_at = self._next_retry_at(
                            recovered_at, max(1, int(item.attempt_count or 0))
                        )
                        item.updated_at = recovered_at
                row.status = "recovery_required"
                row.lease_owner = None
                row.lease_expires_at = None
                row.updated_at = recovered_at
                count += 1
        return count

    def claim_next_plan(
        self,
        *,
        lease_owner: str,
        lease_seconds: int = 30,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        claimed_at = _aware(now or datetime.now(timezone.utc))
        with self.database.session() as session:
            row = session.scalars(
                select(CalendarMutationPlan)
                .where(
                    or_(
                        CalendarMutationPlan.status == "queued",
                        and_(
                            CalendarMutationPlan.status == "recovery_required",
                            ~exists(
                                select(CalendarMutationPlanItem.id).where(
                                    CalendarMutationPlanItem.plan_id
                                    == CalendarMutationPlan.id,
                                    CalendarMutationPlanItem.next_retry_at.is_not(None),
                                    CalendarMutationPlanItem.next_retry_at > claimed_at,
                                )
                            ),
                        ),
                        (
                            (CalendarMutationPlan.status == "running")
                            & or_(
                                CalendarMutationPlan.lease_expires_at.is_(None),
                                CalendarMutationPlan.lease_expires_at <= claimed_at,
                            )
                        ),
                    )
                )
                .order_by(
                    CalendarMutationPlan.run_requested_at,
                    CalendarMutationPlan.created_at,
                )
                .with_for_update(skip_locked=True)
            ).first()
            if row is None:
                return None
            if row.status == "running":
                for item in self._items(session, row.id):
                    if item.status in {"creating", "deleting"}:
                        item.status = "outcome_unknown"
                        item.error_code = "runner_lease_expired"
                        item.next_retry_at = self._next_retry_at(
                            claimed_at, max(1, int(item.attempt_count or 0))
                        )
                        item.updated_at = claimed_at
            row.status = "running"
            row.lease_owner = str(lease_owner)[:64]
            row.lease_expires_at = claimed_at + timedelta(
                seconds=max(5, int(lease_seconds))
            )
            row.last_progress_at = claimed_at
            row.updated_at = claimed_at
            session.flush()
            return self._view(row, self._items(session, row.id))

    def claim_next_item(
        self,
        plan_id: uuid.UUID | str,
        *,
        lease_owner: str,
        lease_seconds: int = 30,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        claimed_at = _aware(now or datetime.now(timezone.utc))
        with self.database.session() as session:
            plan = session.get(
                CalendarMutationPlan, uuid.UUID(str(plan_id)), with_for_update=True
            )
            if (
                plan is None
                or plan.status != "running"
                or plan.lease_owner != str(lease_owner)[:64]
                or (
                    plan.lease_expires_at is not None
                    and _aware(plan.lease_expires_at) <= claimed_at
                )
            ):
                return None
            item = session.scalars(
                select(CalendarMutationPlanItem)
                .where(
                    CalendarMutationPlanItem.plan_id == plan.id,
                    or_(
                        CalendarMutationPlanItem.status.in_([
                            "pending", "outcome_unknown"
                        ]),
                        and_(
                            CalendarMutationPlanItem.status == "failed",
                            CalendarMutationPlanItem.error_code
                            == "calendar_provider_unavailable",
                        ),
                    ),
                    or_(
                        CalendarMutationPlanItem.next_retry_at.is_(None),
                        CalendarMutationPlanItem.next_retry_at <= claimed_at,
                    ),
                )
                .order_by(CalendarMutationPlanItem.item_index)
                .with_for_update(skip_locked=True)
            ).first()
            if item is None:
                return None
            reconcile = item.status == "outcome_unknown"
            item.status = "creating" if item.operation == "create" else "deleting"
            item.attempt_count = int(item.attempt_count or 0) + 1
            item.last_attempt_at = claimed_at
            item.next_retry_at = None
            item.updated_at = claimed_at
            plan.lease_expires_at = claimed_at + timedelta(
                seconds=max(5, int(lease_seconds))
            )
            plan.last_progress_at = claimed_at
            plan.updated_at = claimed_at
            session.flush()
            value = self._item_view(item)
            value["participant_id"] = str(plan.participant_id)
            value["reconcile"] = reconcile
            return value

    def record_item_success(
        self,
        plan_id: uuid.UUID | str,
        item_id: uuid.UUID | str,
        *,
        lease_owner: str,
        provider_event_id: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        completed_at = _aware(now or datetime.now(timezone.utc))
        with self.database.session() as session:
            plan = session.get(
                CalendarMutationPlan, uuid.UUID(str(plan_id)), with_for_update=True
            )
            item = session.get(
                CalendarMutationPlanItem, uuid.UUID(str(item_id)), with_for_update=True
            )
            if (
                plan is None
                or item is None
                or item.plan_id != plan.id
                or plan.status != "running"
                or plan.lease_owner != str(lease_owner)[:64]
                or item.status not in {"creating", "deleting"}
            ):
                return None
            normalized_provider_id = str(provider_event_id or "").strip() or None
            if item.operation == "create" and normalized_provider_id is None:
                raise ValueError("created Calendar item requires provider_event_id")
            item.status = "succeeded"
            item.provider_event_id = normalized_provider_id or item.provider_event_id
            item.error_code = None
            item.error_detail = None
            item.next_retry_at = None
            item.completed_at = completed_at
            item.updated_at = completed_at
            plan.last_progress_at = completed_at
            plan.updated_at = completed_at
            session.flush()
            return self._item_view(item)

    def record_item_failure(
        self,
        plan_id: uuid.UUID | str,
        item_id: uuid.UUID | str,
        *,
        lease_owner: str,
        error_code: str,
        error_detail: str | None = None,
        outcome_unknown: bool = False,
        retryable: bool = False,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        failed_at = _aware(now or datetime.now(timezone.utc))
        with self.database.session() as session:
            plan = session.get(
                CalendarMutationPlan, uuid.UUID(str(plan_id)), with_for_update=True
            )
            item = session.get(
                CalendarMutationPlanItem, uuid.UUID(str(item_id)), with_for_update=True
            )
            if (
                plan is None
                or item is None
                or item.plan_id != plan.id
                or plan.status != "running"
                or plan.lease_owner != str(lease_owner)[:64]
                or item.status not in {"creating", "deleting"}
            ):
                return None
            item.status = "outcome_unknown" if outcome_unknown else "failed"
            item.error_code = str(error_code)[:128]
            item.error_detail = str(error_detail or "")[:1000] or None
            should_retry = outcome_unknown or retryable
            item.next_retry_at = (
                self._next_retry_at(failed_at, max(1, int(item.attempt_count or 0)))
                if should_retry
                else None
            )
            item.completed_at = None if should_retry else failed_at
            item.updated_at = failed_at
            plan.last_progress_at = failed_at
            plan.updated_at = failed_at
            if should_retry:
                plan.status = "recovery_required"
                plan.lease_owner = None
                plan.lease_expires_at = None
            session.flush()
            return self._item_view(item)

    def finalize(
        self,
        plan_id: uuid.UUID | str,
        *,
        lease_owner: str,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        finalized_at = _aware(now or datetime.now(timezone.utc))
        with self.database.session() as session:
            row = session.get(
                CalendarMutationPlan, uuid.UUID(str(plan_id)), with_for_update=True
            )
            if row is None:
                return None
            items = self._items(session, row.id)
            if row.status == "recovery_required":
                return self._view(row, items)
            if row.status != "running" or row.lease_owner != str(lease_owner)[:64]:
                return None
            statuses = [item.status for item in items]
            if any(status == "outcome_unknown" for status in statuses):
                row.status = "recovery_required"
            elif any(
                status in {"pending", "creating", "deleting"} for status in statuses
            ):
                return self._view(row, items)
            else:
                succeeded_count = statuses.count("succeeded")
                failed_count = len(statuses) - succeeded_count
                row.status = "succeeded" if failed_count == 0 else "partial_failed"
                row.result_json = {
                    "succeeded_count": succeeded_count,
                    "failed_count": failed_count,
                    "errors": [item.error_code for item in items if item.error_code],
                }
                row.completed_at = finalized_at
            row.lease_owner = None
            row.lease_expires_at = None
            row.last_progress_at = finalized_at
            row.updated_at = finalized_at
            session.flush()
            return self._view(row, items)

    def get(self, plan_id: uuid.UUID | str) -> dict[str, Any] | None:
        with self.database.session() as session:
            row = session.get(CalendarMutationPlan, uuid.UUID(str(plan_id)))
            if row is None:
                return None
            return self._view(row, self._items(session, row.id))

    def pending_completion_presentations(
        self, *, limit: int = 20, now: datetime | None = None
    ) -> list[dict[str, Any]]:
        current = _aware(now or datetime.now(timezone.utc))
        with self.database.session() as session:
            rows = list(
                session.scalars(
                    select(CalendarMutationPlan)
                    .where(
                        CalendarMutationPlan.status.in_(["succeeded", "partial_failed"]),
                        CalendarMutationPlan.completion_presented_at.is_(None),
                    )
                    .order_by(CalendarMutationPlan.updated_at)
                    .limit(100)
                )
            )
            due = [
                row
                for row in rows
                if self._completion_presentation_due(
                    row.updated_at, row.completion_presentation_attempts, current
                )
            ]
            return [
                self._view(row, self._items(session, row.id))
                for row in due[: max(1, min(int(limit), 100))]
            ]

    def mark_completion_presented(
        self,
        plan_id: uuid.UUID | str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        presented_at = _aware(now or datetime.now(timezone.utc))
        with self.database.session() as session:
            row = session.get(
                CalendarMutationPlan, uuid.UUID(str(plan_id)), with_for_update=True
            )
            if row is None or row.status not in {"succeeded", "partial_failed"}:
                return None
            row.completion_presented_at = presented_at
            row.completion_presentation_error = None
            row.completion_presentation_attempts = int(
                row.completion_presentation_attempts or 0
            ) + 1
            row.updated_at = presented_at
            session.flush()
            return self._view(row, self._items(session, row.id))

    def mark_completion_presentation_failed(
        self,
        plan_id: uuid.UUID | str,
        *,
        error_code: str,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        failed_at = _aware(now or datetime.now(timezone.utc))
        with self.database.session() as session:
            row = session.get(
                CalendarMutationPlan, uuid.UUID(str(plan_id)), with_for_update=True
            )
            if row is None or row.status not in {"succeeded", "partial_failed"}:
                return None
            row.completion_presentation_error = str(error_code)[:256]
            row.completion_presentation_attempts = int(
                row.completion_presentation_attempts or 0
            ) + 1
            row.updated_at = failed_at
            session.flush()
            return self._view(row, self._items(session, row.id))

    def complete(
        self,
        plan_id: uuid.UUID | str,
        *,
        result: dict[str, Any],
        succeeded: bool,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        """Legacy completion entry point retained for old integrations."""

        completed_at = _aware(now or datetime.now(timezone.utc))
        with self.database.session() as session:
            row = session.get(
                CalendarMutationPlan, uuid.UUID(str(plan_id)), with_for_update=True
            )
            if row is None or row.status not in {"running", "queued"}:
                return None
            row.status = "succeeded" if succeeded else "partial_failed"
            row.result_json = dict(result)
            row.updated_at = completed_at
            row.completed_at = completed_at
            row.lease_owner = None
            row.lease_expires_at = None
            session.flush()
            return self._view(row, self._items(session, row.id))
