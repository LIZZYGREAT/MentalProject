"""Durable background runner for course schedule Calendar imports."""

from __future__ import annotations

import asyncio
from datetime import datetime
import logging
from typing import Any
import uuid

from app.domain.course_schedule_recurrence import CalendarWrite, CalendarWriteKind
from app.integrations.feishu.calendar import (
    CalendarMutationOutcomeUnknown,
)
from app.integrations.feishu.cards import course_schedule_result_card


logger = logging.getLogger(__name__)


class CourseScheduleImportRunner:
    """Claim queued imports and make each Calendar mutation durable."""

    def __init__(
        self,
        imports: Any,
        *,
        sender: Any = None,
        max_concurrency: int = 1,
        poll_interval_seconds: float = 1.0,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("course schedule import concurrency must be positive")
        self.imports = imports
        self.drafts = imports.drafts
        self.calendar = imports.calendar
        self.sender = sender
        self.max_concurrency = min(2, int(max_concurrency))
        self.poll_interval_seconds = max(0.05, float(poll_interval_seconds))
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake_event: asyncio.Event | None = None
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self._startup_recovery_done = False

    def start(self) -> None:
        if self._closed:
            raise RuntimeError("course schedule import runner is closed")
        loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not loop:
            raise RuntimeError("course schedule import runner is already bound")
        self._loop = loop
        if self._wake_event is None:
            self._wake_event = asyncio.Event()
        if self._task is None:
            self._task = asyncio.create_task(
                self.run_forever(), name="course-schedule-import-runner"
            )
        self.wake()

    def wake(self) -> None:
        loop = self._loop
        event = self._wake_event
        if loop is None or event is None or loop.is_closed() or self._closed:
            return
        try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            current = None
        if current is loop:
            event.set()
        else:
            loop.call_soon_threadsafe(event.set)

    async def run_once(self) -> int:
        """Drain the currently claimable durable imports once."""

        claimed: list[dict[str, Any]] = []
        for _ in range(self.max_concurrency):
            draft = await asyncio.to_thread(self.drafts.claim_next_import)
            if draft is None:
                break
            claimed.append(draft)
        if not claimed:
            return 0
        await asyncio.gather(*(self._run_import(draft) for draft in claimed))
        return len(claimed)

    async def recover_startup(self) -> int:
        """Resume queued jobs and expired running leases after process start."""

        await self._run_startup_recovery()
        return await self.run_once()

    async def run_forever(self) -> None:
        event = self._wake_event
        if event is None:
            self._wake_event = asyncio.Event()
            event = self._wake_event
        try:
            while not self._closed:
                event.clear()
                try:
                    if not self._startup_recovery_done:
                        await self._run_startup_recovery()
                    await self.run_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("course_schedule_import_runner_scan_failed")
                if self._closed:
                    break
                try:
                    await asyncio.wait_for(event.wait(), timeout=self.poll_interval_seconds)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            raise

    async def _run_startup_recovery(self) -> int:
        if self._startup_recovery_done:
            return 0
        recovered = await asyncio.to_thread(
            self.drafts.requeue_startup_recoverables
        )
        self._startup_recovery_done = True
        if recovered:
            self.wake()
        return recovered

    async def _run_import(self, draft: dict[str, Any]) -> None:
        import_id = draft["id"]
        participant_id = uuid.UUID(str(draft["participant_id"]))
        writes = [
            self._calendar_write(row)
            for row in draft.get("writes") or []
        ]
        writes_by_item: dict[str, list[CalendarWrite]] = {}
        for row, write in zip(draft.get("writes") or [], writes):
            writes_by_item.setdefault(str(row["item_id"]), []).append(write)
        all_dates = {
            target
            for write in writes
            for target in write.affected_dates
        }
        reconciliation = None
        try:
            reconciliation = await self.imports._prepare_reconciliation(
                participant_id, draft, all_dates, writes_by_item
            )
        except Exception:
            # Calendar writes remain durable even if the optional forecast
            # reconciliation record cannot be prepared.
            logger.exception(
                "course_schedule_import_reconciliation_prepare_failed import_id=%s",
                import_id,
            )

        for row in draft.get("writes") or []:
            claimed = await asyncio.to_thread(
                self.drafts.claim_write, import_id, row["id"]
            )
            if claimed is None:
                continue
            try:
                create = (
                    self.calendar.create_recurring_event
                    if claimed["write_kind"] == CalendarWriteKind.RECURRING
                    else self.calendar.create_single_event
                )
                create_args: dict[str, Any] = {
                    "summary": claimed["summary"],
                    "start_time": datetime.fromisoformat(claimed["start_time"]),
                    "end_time": datetime.fromisoformat(claimed["end_time"]),
                    "description": claimed.get("description") or "",
                    "source_message_id": claimed["source_identity"],
                }
                if claimed["write_kind"] == CalendarWriteKind.RECURRING:
                    create_args["recurrence"] = claimed.get("recurrence")
                created = await create(participant_id, **create_args)
                provider_event_id = str((created or {}).get("id") or "")
                if not provider_event_id.strip():
                    await asyncio.to_thread(
                        self.drafts.record_write_failure,
                        import_id,
                        claimed["id"],
                        error_code="provider_event_id_missing",
                        outcome_unknown=True,
                    )
                    logger.warning(
                        "course_schedule_calendar_write_provider_id_missing "
                        "import_id=%s write_id=%s",
                        import_id,
                        claimed["id"],
                    )
                    continue
                await asyncio.to_thread(
                    self.drafts.record_write_created,
                    import_id,
                    claimed["id"],
                    provider_event_id,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                unknown = self._outcome_unknown(exc)
                error_code = (
                    "calendar_not_connected"
                    if isinstance(exc, PermissionError)
                    else type(exc).__name__
                )
                await asyncio.to_thread(
                    self.drafts.record_write_failure,
                    import_id,
                    claimed["id"],
                    error_code=error_code,
                    outcome_unknown=unknown,
                )
                logger.warning(
                    "course_schedule_calendar_write_failed import_id=%s write_id=%s "
                    "error_class=%s outcome_unknown=%s",
                    import_id,
                    claimed["id"],
                    type(exc).__name__,
                    unknown,
                )
            finally:
                await asyncio.to_thread(self.drafts.renew_run_lease, import_id)

        final = await asyncio.to_thread(
            self.drafts.finalize_queued_import, import_id
        )
        created_dates = await asyncio.to_thread(
            self.drafts.created_write_dates, import_id
        )
        final_writes = final.get("writes") or []
        outcome_unknown = any(
            row.get("status") == "create_outcome_unknown" for row in final_writes
        )
        try:
            await self.imports._finish_reconciliation(
                reconciliation,
                any_success=bool(created_dates),
                outcome_unknown=outcome_unknown,
            )
            if created_dates:
                await self.imports._reconcile_forecasts(
                    participant_id,
                    created_dates,
                    reconciliation=reconciliation,
                )
        except Exception:
            # Provider and import state are already durable; reconciliation can
            # be retried by the existing mutation refresh recovery queue.
            logger.exception(
                "course_schedule_import_reconciliation_failed import_id=%s",
                import_id,
            )
        await self._present_completion(final)

    @staticmethod
    def _outcome_unknown(exc: Exception) -> bool:
        return isinstance(exc, CalendarMutationOutcomeUnknown) or type(exc).__name__.endswith(
            "OutcomeUnknown"
        )

    @staticmethod
    def _calendar_write(row: dict[str, Any]) -> CalendarWrite:
        from datetime import date

        return CalendarWrite(
            summary=row["summary"],
            start_time=datetime.fromisoformat(row["start_time"]),
            end_time=datetime.fromisoformat(row["end_time"]),
            description=row.get("description") or "",
            write_kind=row["write_kind"],
            recurrence=row.get("recurrence"),
            recurrence_interval=None,
            occurrence_identity=row["occurrence_identity"],
            affected_dates=tuple(
                date.fromisoformat(str(value)) for value in row.get("affected_dates") or []
            ),
        )

    async def _present_completion(self, draft: dict[str, Any]) -> None:
        message = self.imports._result(draft)
        card = course_schedule_result_card(
            message["reply_text"],
            status=draft.get("status"),
            import_id=draft.get("id"),
            error=message.get("error"),
            recurrence_strategy=draft.get("recurrence_strategy"),
        )
        sender = self.sender
        if sender is None:
            return
        message_id = draft.get("status_card_message_id")
        if not message_id:
            return
        try:
            await asyncio.to_thread(sender.update_card, message_id, card)
        except Exception:
            logger.exception(
                "course_schedule_import_completion_card_update_failed import_id=%s",
                draft.get("id"),
            )
            chat_id = draft.get("status_card_chat_id")
            if chat_id:
                try:
                    await asyncio.to_thread(sender.send_text, chat_id, message["reply_text"])
                except Exception:
                    logger.exception(
                        "course_schedule_import_completion_text_notice_failed import_id=%s",
                        draft.get("id"),
                    )

    async def close(self) -> None:
        self._closed = True
        self.wake()
        task = self._task
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._task = None
        self._loop = None
        self._wake_event = None
