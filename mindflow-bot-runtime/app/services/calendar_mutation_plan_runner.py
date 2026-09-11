"""Durable background runner for participant-confirmed Calendar batches."""

from __future__ import annotations

import asyncio
import logging
from typing import Any
import uuid

from app.integrations.feishu.calendar import (
    CalendarMutationOutcomeUnknown,
    CalendarProviderUnavailable,
)
from app.integrations.feishu.cards import card_action_result_card


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

    async def run_once(self) -> int:
        plan = await asyncio.to_thread(
            self.plans.claim_next_plan,
            lease_owner=self.owner,
            lease_seconds=self.lease_seconds,
        )
        if plan is None:
            return 0
        await self._run_plan(plan)
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

    async def _run_plan(self, plan: dict[str, Any]) -> None:
        plan_id = str(plan["id"])
        while True:
            item = await asyncio.to_thread(
                self.plans.claim_next_item,
                plan_id,
                lease_owner=self.owner,
                lease_seconds=self.lease_seconds,
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
                await asyncio.to_thread(
                    self.plans.record_item_failure,
                    plan_id,
                    item["id"],
                    lease_owner=self.owner,
                    error_code=type(exc).__name__,
                    error_detail=str(exc),
                    outcome_unknown=unknown,
                )
                logger.warning(
                    "calendar_mutation_plan_item_failed plan_id=%s item_index=%s "
                    "outcome_unknown=%s",
                    plan_id,
                    item["item_index"],
                    unknown,
                    exc_info=True,
                )
                if unknown:
                    return

        final = await asyncio.to_thread(
            self.plans.finalize, plan_id, lease_owner=self.owner
        )
        if final is not None and final.get("status") in {
            "succeeded",
            "partial_failed",
        }:
            await self._present(final)

    @staticmethod
    def _outcome_unknown(exc: Exception) -> bool:
        return (
            isinstance(exc, (CalendarMutationOutcomeUnknown, CalendarProviderUnavailable))
            or type(exc).__name__.endswith("OutcomeUnknown")
        )

    async def _present(self, plan: dict[str, Any]) -> None:
        sender = self.sender
        message_id = str(plan.get("status_card_message_id") or "").strip()
        if sender is None or not message_id:
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
        try:
            await asyncio.to_thread(
                sender.update_card,
                message_id,
                card_action_result_card(message=message),
            )
        except Exception:
            # Updating the same callback card is idempotent and can safely be
            # retried by a future presentation recovery enhancement.
            logger.exception(
                "calendar_mutation_plan_result_presentation_failed plan_id=%s",
                plan.get("id"),
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
