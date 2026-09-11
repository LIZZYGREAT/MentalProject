"""Durable background runner for participant-confirmed Calendar batches."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime
from typing import Any
import uuid

from app.integrations.feishu.calendar import (
    CalendarMutationOutcomeUnknown,
    CalendarProviderUnavailable,
)
from app.integrations.feishu.cards import card_action_result_card
from app.integrations.feishu.client import FeishuSendError
from app.repositories import RuntimeIncidentRepository


logger = logging.getLogger(__name__)


class CalendarMutationPlanRunner:
    """Execute each provider effect from a durable per-item ledger."""

    def __init__(
        self,
        plans: Any,
        item_executor: Any,
        *,
        sender: Any = None,
        poll_interval_seconds: float = 1.0,
        lease_seconds: int = 30,
    ) -> None:
        self.plans = plans
        self.item_executor = item_executor
        self.sender = sender
        self.poll_interval_seconds = max(0.05, float(poll_interval_seconds))
        self.lease_seconds = max(5, int(lease_seconds))
        self.owner = f"calendar-plan-runner:{uuid.uuid4()}"
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake_event: asyncio.Event | None = None
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self._startup_recovery_done = False

    def start(self) -> None:
        if self._closed:
            raise RuntimeError("calendar mutation plan runner is closed")
        loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not loop:
            raise RuntimeError("calendar mutation plan runner is already bound")
        self._loop = loop
        if self._wake_event is None:
            self._wake_event = asyncio.Event()
        if self._task is None:
            self._task = asyncio.create_task(
                self.run_forever(), name="calendar-mutation-plan-runner"
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

    async def recover_startup(self) -> int:
        recovered = await asyncio.to_thread(self.plans.recover_stale)
        self._startup_recovery_done = True
        return recovered + await self.run_once()

    async def run_once(self, *, now: datetime | None = None) -> int:
        plan = await asyncio.to_thread(
            self.plans.claim_next_plan,
            lease_owner=self.owner,
            lease_seconds=self.lease_seconds,
            now=now,
        )
        if plan is None:
            pending = await asyncio.to_thread(
                self.plans.pending_completion_presentations, limit=1, now=now
            )
            if pending:
                await self._present(pending[0], now=now)
                return 1
            return 0
        await self._run_plan(plan, now=now)
        return 1

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
                        await asyncio.to_thread(self.plans.recover_stale)
                        self._startup_recovery_done = True
                    await self.run_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("calendar_mutation_plan_runner_scan_failed")
                if self._closed:
                    break
                try:
                    await asyncio.wait_for(event.wait(), self.poll_interval_seconds)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            raise

    async def _run_plan(
        self, plan: dict[str, Any], *, now: datetime | None = None
    ) -> None:
        plan_id = str(plan["id"])
        while True:
            item = await asyncio.to_thread(
                self.plans.claim_next_item,
                plan_id,
                lease_owner=self.owner,
                lease_seconds=self.lease_seconds,
                now=now,
            )
            if item is None:
                break
            try:
                result = await self.item_executor(plan, item)
                if not result.get("ok"):
                    await asyncio.to_thread(
                        self.plans.record_item_failure,
                        plan_id,
                        item["id"],
                        lease_owner=self.owner,
                        error_code=str(result.get("error") or "mutation_failed"),
                    )
                    continue
                provider_event_id = None
                if item["operation"] == "create":
                    provider_event_id = str(
                        dict(result.get("created") or {}).get("id") or ""
                    ).strip()
                    if not provider_event_id:
                        raise CalendarMutationOutcomeUnknown(
                            "Calendar create returned no event id",
                            request_kind="create_event",
                        )
                await asyncio.to_thread(
                    self.plans.record_item_success,
                    plan_id,
                    item["id"],
                    lease_owner=self.owner,
                    provider_event_id=provider_event_id,
                )
            except asyncio.CancelledError:
                # The active item remains fenced by the plan lease. A later
                # owner converts it to outcome_unknown after lease expiry.
                raise
            except Exception as exc:
                unknown = self._outcome_unknown(exc)
                provider_unavailable = isinstance(exc, CalendarProviderUnavailable)
                recorded = await asyncio.to_thread(
                    self.plans.record_item_failure,
                    plan_id,
                    item["id"],
                    lease_owner=self.owner,
                    error_code=(
                        "calendar_provider_unavailable"
                        if provider_unavailable
                        else type(exc).__name__
                    ),
                    error_detail=str(exc),
                    outcome_unknown=unknown,
                    retryable=provider_unavailable,
                )
                logger.warning(
                    "calendar_mutation_plan_item_failed plan_id=%s item_index=%s "
                    "outcome_unknown=%s provider_unavailable=%s",
                    plan_id,
                    item["item_index"],
                    unknown,
                    provider_unavailable,
                    exc_info=True,
                )
                if (
                    unknown
                    and recorded is not None
                    and int(recorded.get("attempt_count") or 0)
                    >= self.plans._MAX_OUTCOME_UNKNOWN_ATTEMPTS
                ):
                    await self._record_recovery_incident(plan, item, exc)
                if unknown or provider_unavailable:
                    return

        final = await asyncio.to_thread(
            self.plans.finalize, plan_id, lease_owner=self.owner, now=now
        )
        if final is not None and final.get("status") in {
            "succeeded",
            "partial_failed",
        }:
            await self._present(final, now=now)

    @staticmethod
    def _outcome_unknown(exc: Exception) -> bool:
        return isinstance(exc, CalendarMutationOutcomeUnknown) or type(exc).__name__.endswith(
            "OutcomeUnknown"
        )

    async def _record_recovery_incident(
        self, plan: dict[str, Any], item: dict[str, Any], exc: Exception
    ) -> None:
        repository = RuntimeIncidentRepository(self.plans.database)
        try:
            already_reported = await asyncio.to_thread(
                repository.has_incident,
                subsystem="calendar_mutation_plan",
                event_name="outcome_unknown_retry_exhausted",
                details_match={
                    "plan_id": str(plan["id"]),
                    "item_id": str(item["id"]),
                },
            )
            if already_reported:
                return
            await asyncio.to_thread(
                repository.record,
                severity="error",
                subsystem="calendar_mutation_plan",
                event_name="outcome_unknown_retry_exhausted",
                summary="Calendar mutation plan outcome remains unknown after retry backoff",
                participant_id=uuid.UUID(str(plan["participant_id"])),
                error_code=type(exc).__name__,
                error_class=type(exc).__name__,
                details={
                    "plan_id": str(plan["id"]),
                    "item_id": str(item["id"]),
                    "item_index": item["item_index"],
                    "source_identity": item["source_identity"],
                    "attempt_count": item.get("attempt_count"),
                },
            )
        except Exception:
            logger.exception(
                "calendar_mutation_plan_recovery_incident_failed plan_id=%s",
                plan.get("id"),
            )

    async def _record_presentation_incident(
        self, plan: dict[str, Any], recorded: dict[str, Any]
    ) -> None:
        repository = RuntimeIncidentRepository(self.plans.database)
        error_code = str(recorded.get("completion_presentation_error") or "")
        try:
            already_reported = await asyncio.to_thread(
                repository.has_incident,
                subsystem="calendar_mutation_plan",
                event_name="completion_presentation_retry_exhausted",
                details_match={"plan_id": str(plan["id"])},
            )
            if already_reported:
                return
            await asyncio.to_thread(
                repository.record,
                severity="error",
                subsystem="calendar_mutation_plan",
                event_name="completion_presentation_retry_exhausted",
                summary=(
                    "Calendar mutation plan completion card is still unpresented "
                    "after retry backoff"
                ),
                participant_id=uuid.UUID(str(plan["participant_id"])),
                error_code=error_code[:128] or None,
                error_class=error_code[:128] or None,
                details={
                    "plan_id": str(plan["id"]),
                    "completion_presentation_attempts": recorded.get(
                        "completion_presentation_attempts"
                    ),
                    "completion_presentation_error": error_code,
                },
            )
        except Exception:
            logger.exception(
                "calendar_mutation_plan_presentation_incident_failed plan_id=%s",
                plan.get("id"),
            )

    async def _present(
        self, plan: dict[str, Any], *, now: datetime | None = None
    ) -> None:
        sender = self.sender
        message_id = str(plan.get("status_card_message_id") or "").strip()
        mark_failed = getattr(self.plans, "mark_completion_presentation_failed", None)
        mark_presented = getattr(self.plans, "mark_completion_presented", None)

        async def failed(error_code: str) -> None:
            if not callable(mark_failed):
                return
            recorded = await asyncio.to_thread(
                mark_failed, plan["id"], error_code=error_code, now=now
            )
            if (
                recorded is not None
                and int(recorded.get("completion_presentation_attempts") or 0)
                >= self.plans._MAX_COMPLETION_PRESENTATION_ATTEMPTS
            ):
                await self._record_presentation_incident(plan, recorded)

        if sender is None:
            await failed("sender_unavailable")
            return
        result = dict(plan.get("result") or {})
        succeeded_count = int(result.get("succeeded_count") or 0)
        failed_count = int(result.get("failed_count") or 0)
        verb = "添加" if plan.get("operation") == "create" else "删除"
        message = (
            f"已{verb} {succeeded_count} 个日程。"
            if failed_count == 0
            else f"已{verb} {succeeded_count} 个，另有 {failed_count} 个未完成。"
        )
        card = card_action_result_card(message=message)
        presented = False
        try:
            if message_id:
                await asyncio.to_thread(sender.update_card, message_id, card)
                presented = True
            elif plan.get("status_card_chat_id"):
                await asyncio.to_thread(
                    sender.send_card,
                    plan["status_card_chat_id"],
                    card,
                    message_uuid=self._presentation_message_uuid(plan["id"]),
                )
                presented = True
        except FeishuSendError as exc:
            if (
                message_id
                and exc.operation == "update_card"
                and exc.replacement_allowed
                and not exc.retryable
                and plan.get("status_card_chat_id")
            ):
                try:
                    await asyncio.to_thread(
                        sender.send_card,
                        plan["status_card_chat_id"],
                        card,
                        message_uuid=self._presentation_message_uuid(plan["id"]),
                    )
                    presented = True
                except Exception as fallback_exc:
                    logger.exception(
                        "calendar_mutation_plan_result_fallback_failed plan_id=%s",
                        plan.get("id"),
                    )
                    await failed(type(fallback_exc).__name__)
            else:
                await failed(type(exc).__name__)
        except Exception as exc:
            logger.exception(
                "calendar_mutation_plan_result_presentation_failed plan_id=%s",
                plan.get("id"),
            )
            await failed(type(exc).__name__)
        if presented and callable(mark_presented):
            await asyncio.to_thread(mark_presented, plan["id"], now=now)
        elif not presented and not callable(mark_failed):
            logger.error(
                "calendar_mutation_plan_result_presentation_unavailable plan_id=%s",
                plan.get("id"),
            )

    @staticmethod
    def _presentation_message_uuid(plan_id: str) -> str:
        digest = hashlib.sha256(
            f"mindflow:calendar-plan-result:{plan_id}".encode("utf-8")
        ).hexdigest()
        return f"mindflow-{digest[:40]}"

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
