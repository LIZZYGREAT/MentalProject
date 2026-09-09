"""Durable background runner for course schedule Calendar imports."""

from __future__ import annotations

import asyncio
from datetime import date, datetime
import logging
from typing import Any
import uuid

from app.domain.course_schedule_recurrence import CalendarWrite, CalendarWriteKind
from app.integrations.feishu.calendar import (
    CalendarProviderUnavailable,
    CalendarMutationRejected,
    CalendarMutationOutcomeUnknown,
)
from app.integrations.feishu.cards import course_schedule_result_card
from app.repositories_course_schedule import CourseScheduleProviderIdentityConflict


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

        reconciliation_write = await asyncio.to_thread(
            self.drafts.claim_next_cancellation_reconciliation
        )
        if reconciliation_write is not None:
            await self._run_create_reconciliation(reconciliation_write)
            first_compensation = await asyncio.to_thread(
                self.drafts.claim_next_compensation
            )
            if first_compensation is not None:
                await self._run_compensations(first_compensation)
            rollback_refresh = await asyncio.to_thread(
                self.drafts.claim_next_rollback_refresh
            )
            if rollback_refresh is not None:
                await self._run_rollback_refresh(rollback_refresh)
            return 1

        # Cleanup is deliberately first priority. A cancellation fence must
        # converge provider state before another import is allowed to run.
        first_compensation = await asyncio.to_thread(
            self.drafts.claim_next_compensation
        )
        if first_compensation is not None:
            await self._run_compensations(first_compensation)
            return 1

        rollback_refresh = await asyncio.to_thread(
            self.drafts.claim_next_rollback_refresh
        )
        if rollback_refresh is not None:
            await self._run_rollback_refresh(rollback_refresh)
            return 1

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
        await asyncio.to_thread(self.drafts.normalize_identity_conflicts)
        recovered = await asyncio.to_thread(
            self.drafts.requeue_startup_recoverables
        )
        cancellation_recovered = await asyncio.to_thread(
            self.drafts.recover_stale_cancellation_work
        )
        pending_presentations = await asyncio.to_thread(
            self.drafts.pending_completion_presentations
        )
        for draft in pending_presentations:
            await self._present_completion(draft)
        self._startup_recovery_done = True
        if recovered or cancellation_recovered:
            self.wake()
        return recovered + cancellation_recovered

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
        reconciliation_claim_token: uuid.UUID | None = None
        reconciliation_owner_active = True
        try:
            reconciliation = await self.imports._prepare_reconciliation(
                participant_id, draft, all_dates, writes_by_item
            )
            if reconciliation is not None:
                reconciliation_claim_token = uuid.uuid4()
                repository = self.imports.mutation_refresh.reconciliations
                claim = getattr(repository, "claim_processing", None)
                if callable(claim):
                    claimed_reconciliation = await asyncio.to_thread(
                        claim,
                        reconciliation["id"],
                        claim_token=reconciliation_claim_token,
                    )
                    if claimed_reconciliation is None:
                        # Another durable owner already took this row. The
                        # provider write runner may finish its own ledger
                        # work, but it must not perform downstream work.
                        reconciliation_claim_token = None
                        reconciliation_owner_active = False
                    else:
                        reconciliation = claimed_reconciliation
                else:
                    # Compatibility for injected repositories from before
                    # processing leases existed.
                    reconciliation_claim_token = None
        except Exception:
            if reconciliation is not None and reconciliation_claim_token is not None:
                # The claim outcome is unknown; do not guess that this runner
                # owns the reconciliation during the final hand-off.
                reconciliation_owner_active = False
                reconciliation_claim_token = None
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
            except CourseScheduleProviderIdentityConflict as exc:
                logger.error(
                    "course_schedule_calendar_write_provider_identity_conflict "
                    "import_id=%s write_id=%s existing=%s incoming=%s",
                    import_id,
                    claimed["id"],
                    exc.existing_provider_event_id,
                    exc.incoming_provider_event_id,
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
        identity_conflict = any(
            row.get("status") == "create_identity_conflict"
            for row in final_writes
        )
        try:
            bound_reconciliation = None
            if reconciliation_owner_active:
                bound_reconciliation = await self.imports._finish_reconciliation(
                    reconciliation,
                    effect_dates=created_dates,
                    outcome_unknown=outcome_unknown,
                    outcome_unknown_error=(
                        "CourseScheduleProviderIdentityConflict"
                        if identity_conflict
                        else "CourseScheduleBatchOutcomeUnknown"
                    ),
                    claim_token=reconciliation_claim_token,
                )
            if created_dates and (
                reconciliation is None or bound_reconciliation is not None
            ):
                await self.imports._reconcile_forecasts(
                    participant_id,
                    created_dates,
                    reconciliation=bound_reconciliation,
                )
        except Exception:
            # Provider and import state are already durable; reconciliation can
            # be retried by the existing mutation refresh recovery queue.
            logger.exception(
                "course_schedule_import_reconciliation_failed import_id=%s",
                import_id,
            )
        if final.get("status") in {
            "succeeded", "partial_failed", "cancelled", "cleanup_failed"
        }:
            await self._present_completion(final)

    async def _run_create_reconciliation(self, write: dict[str, Any]) -> None:
        """Replay one already-dispatched create using its original identity."""

        import_id = str(write["import_id"])
        participant_id = uuid.UUID(str(write["participant_id"]))
        try:
            create = (
                self.calendar.create_recurring_event
                if write["write_kind"] == CalendarWriteKind.RECURRING
                else self.calendar.create_single_event
            )
            args: dict[str, Any] = {
                "summary": write["summary"],
                "start_time": datetime.fromisoformat(write["start_time"]),
                "end_time": datetime.fromisoformat(write["end_time"]),
                "description": write.get("description") or "",
                "source_message_id": write["source_identity"],
            }
            if write["write_kind"] == CalendarWriteKind.RECURRING:
                args["recurrence"] = write.get("recurrence")
            created = await create(participant_id, **args)
            provider_event_id = str((created or {}).get("id") or "").strip()
            if not provider_event_id:
                await asyncio.to_thread(
                    self.drafts.record_write_failure,
                    import_id,
                    write["id"],
                    error_code="provider_event_id_missing",
                    outcome_unknown=True,
                )
            else:
                await asyncio.to_thread(
                    self.drafts.record_write_created,
                    import_id,
                    write["id"],
                    provider_event_id,
                )
        except CourseScheduleProviderIdentityConflict:
            logger.error(
                "course_schedule_cancellation_replay_identity_conflict "
                "import_id=%s write_id=%s",
                import_id,
                write["id"],
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await asyncio.to_thread(
                self.drafts.record_write_failure,
                import_id,
                write["id"],
                error_code=(
                    "calendar_not_connected"
                    if isinstance(exc, PermissionError)
                    else type(exc).__name__
                ),
                outcome_unknown=self._outcome_unknown(exc),
            )
            logger.warning(
                "course_schedule_cancellation_replay_failed import_id=%s "
                "write_id=%s outcome_unknown=%s",
                import_id,
                write["id"],
                self._outcome_unknown(exc),
            )
        finally:
            await asyncio.to_thread(self.drafts.renew_run_lease, import_id)
        final = await asyncio.to_thread(
            self.drafts.finalize_cancellation, import_id
        )
        if final.get("status") in {"cancelled", "cleanup_failed"}:
            await self._present_completion(final)

    async def _run_compensations(self, first: dict[str, Any]) -> None:
        """Delete every currently claimable target for one import in order."""

        target: dict[str, Any] | None = first
        while target is not None:
            completed = await self._run_compensation(target)
            if not completed:
                # In particular, do not hot-loop on a transport outcome that
                # remains unknown. Restart/read-back will own the next try.
                break
            target = await asyncio.to_thread(self.drafts.claim_next_compensation)

    async def _run_compensation(self, target: dict[str, Any]) -> bool:
        participant_id = uuid.UUID(str(target["participant_id"]))
        event_id = str(target["provider_event_id"])
        deleted = False
        try:
            if target.get("status") == "delete_outcome_unknown":
                try:
                    await self.calendar.get_event(participant_id, event_id)
                except CalendarMutationRejected as exc:
                    if exc.status_code == 404:
                        deleted = True
                    else:
                        raise
                if not deleted:
                    await self.calendar.delete_event(participant_id, event_id)
                    deleted = True
            else:
                await self.calendar.delete_event(participant_id, event_id)
                deleted = True
        except CalendarMutationRejected as exc:
            if exc.status_code == 404:
                deleted = True
            else:
                await asyncio.to_thread(
                    self.drafts.record_delete_failure,
                    target["id"],
                    error_code=(
                        "calendar_not_connected"
                        if exc.status_code in {401, 403}
                        else type(exc).__name__
                    ),
                )
        except PermissionError:
            await asyncio.to_thread(
                self.drafts.record_delete_failure,
                target["id"],
                error_code="calendar_not_connected",
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await asyncio.to_thread(
                self.drafts.record_delete_failure,
                target["id"],
                error_code=type(exc).__name__,
                outcome_unknown=self._outcome_unknown(exc),
            )

        if not deleted:
            current = await asyncio.to_thread(
                self.drafts.compensations_for_import, target["import_id"]
            )
            current_target = next(
                (item for item in current if item["id"] == target["id"]), target
            )
            return current_target.get("status") == "deleted"

        result = await asyncio.to_thread(
            self.drafts.record_delete_success, target["id"]
        )
        if result is None:
            return False
        final = await asyncio.to_thread(
            self.drafts.finalize_cancellation, target["import_id"]
        )
        if final.get("status") in {"cancelled", "cleanup_failed"}:
            await self._present_completion(final)
        return True

    async def _run_rollback_refresh(self, target: dict[str, Any]) -> bool:
        participant_id = uuid.UUID(str(target["participant_id"]))
        deleted_dates = {
            datetime.fromisoformat(str(value)).date()
            if "T" in str(value)
            else date.fromisoformat(str(value))
            for value in target.get("affected_dates") or []
        }
        try:
            if deleted_dates:
                await self.imports.reconcile_deleted_dates(
                    participant_id, deleted_dates
                )
            completed = await asyncio.to_thread(
                self.drafts.mark_rollback_refresh_completed,
                target["id"],
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await asyncio.to_thread(
                self.drafts.mark_rollback_refresh_retry,
                target["id"],
                error_code=type(exc).__name__,
            )
            logger.exception(
                "course_schedule_import_rollback_forecast_refresh_failed "
                "import_id=%s target_id=%s",
                target["import_id"],
                target["id"],
            )
            return False
        if completed is None:
            return False
        final = await asyncio.to_thread(
            self.drafts.finalize_cancellation, target["import_id"]
        )
        if final.get("status") in {"cancelled", "cleanup_failed"}:
            await self._present_completion(final)
        return True

    @staticmethod
    def _outcome_unknown(exc: Exception) -> bool:
        return (
            isinstance(exc, (CalendarMutationOutcomeUnknown, CalendarProviderUnavailable))
            or type(exc).__name__.endswith("OutcomeUnknown")
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
        import_id = draft.get("id")
        mark_failed = getattr(self.drafts, "mark_completion_presentation_failed", None)
        mark_presented = getattr(self.drafts, "mark_completion_presented", None)

        async def fail(error_code: str) -> None:
            if callable(mark_failed) and import_id:
                await asyncio.to_thread(
                    mark_failed, import_id, error_code=error_code
                )

        async def succeed() -> None:
            if callable(mark_presented) and import_id:
                await asyncio.to_thread(mark_presented, import_id)

        if sender is None:
            await fail("sender_unavailable")
            return
        message_id = draft.get("status_card_message_id")
        chat_id = draft.get("status_card_chat_id")
        if not message_id and not chat_id:
            await fail("presentation_target_missing")
            return
        card_error: Exception | None = None
        presented = False
        try:
            if message_id:
                await asyncio.to_thread(sender.update_card, message_id, card)
                presented = True
        except Exception as exc:
            card_error = exc
            logger.exception(
                "course_schedule_import_completion_card_update_failed import_id=%s",
                draft.get("id"),
            )
        if not presented and chat_id:
            try:
                await asyncio.to_thread(sender.send_text, chat_id, message["reply_text"])
                presented = True
            except Exception:
                logger.exception(
                    "course_schedule_import_completion_text_notice_failed import_id=%s",
                    draft.get("id"),
                )
        if presented:
            await succeed()
        else:
            await fail(
                "completion_card_update_failed"
                if card_error is not None
                else "completion_text_notice_failed"
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
