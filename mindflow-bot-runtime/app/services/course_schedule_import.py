"""Confirmed, participant-scoped course schedule batch Calendar import."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
import logging
from typing import Any
import uuid
from zoneinfo import ZoneInfo

from app.domain.course_schedule_periods import DEFAULT_PERIOD_MAP_VERSION
from app.domain.course_schedule_recurrence import (
    COURSE_IMPORT_PLANNER_VERSION,
    PRESERVE_SCHEDULE_PATTERN,
    CalendarWrite,
    plan_course_writes,
)
from app.repositories_course_schedule import CourseScheduleImportRepository


logger = logging.getLogger(__name__)


class CourseScheduleImportService:
    def __init__(
        self,
        drafts: CourseScheduleImportRepository,
        calendar: Any,
        tokens: Any,
        *,
        timezone_name: str = "Asia/Shanghai",
        forecast_coordinator: Any = None,
        forecast_snapshots: Any = None,
        mutation_refresh: Any = None,
        max_calendar_writes: int = 400,
        queue_notifier: Any = None,
    ) -> None:
        self.drafts = drafts
        self.calendar = calendar
        self.tokens = tokens
        self.timezone = ZoneInfo(timezone_name)
        self.forecast_coordinator = forecast_coordinator
        self.forecast_snapshots = forecast_snapshots
        self.mutation_refresh = mutation_refresh
        self.max_calendar_writes = max(1, int(max_calendar_writes))
        self.queue_notifier = queue_notifier

    async def confirm(
        self,
        participant_id: uuid.UUID,
        import_id: uuid.UUID | str,
        *,
        recurrence_strategy: str | None = None,
        status_card_message_id: str | None = None,
        status_card_chat_id: str | None = None,
    ) -> dict[str, Any]:
        """Validate, plan, and durably queue an import.

        Provider mutations intentionally do not happen here.  This method is on
        the CardAction/HTTP callback path, so all Calendar writes belong to the
        durable background runner below.
        """

        draft = await asyncio.to_thread(
            self.drafts.validate_for_confirmation, participant_id, import_id
        )
        if draft["status"] in {"cancelled", "cancelling", "cleanup_failed"}:
            return self._result(draft)
        if draft["status"] == "succeeded":
            return self._result(draft, already_completed=True)
        if draft["status"] in {"queued", "running"}:
            return self._queued_result(draft)
        if recurrence_strategy is not None:
            draft = await asyncio.to_thread(
                self.drafts.set_recurrence_strategy,
                participant_id,
                import_id,
                recurrence_strategy,
            )
        strategy = str(draft.get("recurrence_strategy") or "")
        if not strategy:
            return {
                "ok": False,
                "error": "recurrence_strategy_required",
                "status": draft["status"],
                "import_id": draft["id"],
                "reply_text": "请先选择按课表周期添加，还是全部拆成单次日程。",
            }
        if not self._calendar_write_enabled(participant_id):
            return {
                "ok": False,
                "error": "calendar_not_connected",
                "status": draft["status"],
                "import_id": draft["id"],
                "recurrence_strategy": strategy,
                "reply_text": (
                    "课程表还没添加\n\n还差一步日历授权。\n"
                    "发送 /calendar 完成授权后，再回来点确认。"
                ),
            }

        writes_by_item: dict[str, list[CalendarWrite]] = {}
        draft_timezone = ZoneInfo(str(draft.get("timezone") or self.timezone.key))
        for item in draft["items"]:
            if item["status"] == "succeeded":
                continue
            writes = plan_course_writes(
                draft,
                item,
                strategy=strategy,
                timezone=draft_timezone,
            )
            writes_by_item[item["id"]] = writes
        planned_writes = sum(len(writes) for writes in writes_by_item.values())
        if planned_writes > self.max_calendar_writes:
            if strategy == PRESERVE_SCHEDULE_PATTERN:
                limit_text = (
                    f"按当前课表周期规则仍需要生成 {planned_writes} 个独立日程，"
                    "超过当前一次导入上限。请取消后拆分课程表重新导入。"
                )
            else:
                limit_text = (
                    f"全部拆成单次日程会生成 {planned_writes} 个日程，"
                    "超过当前一次导入上限。可以改用“按课表周期规则添加”，"
                    "或取消并拆分课程表重新导入。"
                )
            return {
                "ok": False,
                "error": "calendar_write_limit_exceeded",
                "status": draft["status"],
                "import_id": draft["id"],
                "recurrence_strategy": strategy,
                "planned_writes": planned_writes,
                "reply_text": limit_text,
            }
        ledger_payloads = [
            {
                "item_id": item_id,
                "occurrence_identity": write.occurrence_identity,
                "source_identity": self._source_identity(
                    draft,
                    next(item for item in draft["items"] if item["id"] == item_id),
                    write,
                    strategy,
                ),
                "write_kind": write.write_kind,
                "summary": write.summary,
                "description": write.description,
                "start_time": write.start_time,
                "end_time": write.end_time,
                "recurrence": write.recurrence,
                "affected_dates": write.affected_dates,
            }
            for item_id, writes in writes_by_item.items()
            for write in writes
        ]
        queued = await asyncio.to_thread(
            self.drafts.queue_import,
            participant_id,
            import_id,
            recurrence_strategy=strategy,
            writes=ledger_payloads,
            status_card_message_id=status_card_message_id,
            status_card_chat_id=status_card_chat_id,
        )
        if queued.get("identity_conflict"):
            return self._result(queued)
        if queued.get("status") == "succeeded":
            return self._result(queued, already_completed=True)
        if not queued.get("queued") and queued.get("status") == "running":
            return self._queued_result(queued)
        if not queued.get("queued") and queued.get("status") == "queued":
            return self._queued_result(queued)
        if not ledger_payloads:
            final = await asyncio.to_thread(self.drafts.finalize_queued_import, import_id)
            return self._result(final)
        notifier = self.queue_notifier
        if callable(notifier):
            try:
                notifier()
            except Exception:
                logger.exception("course_schedule_import_runner_wakeup_failed")
        return self._queued_result(queued, planned_writes=planned_writes)

    def cancel(
        self, participant_id: uuid.UUID, import_id: uuid.UUID | str
    ) -> dict[str, Any]:
        draft = self.drafts.request_cancel(participant_id, import_id)
        self._wake_runner()
        return self._cancel_result(draft)

    def cancel_or_revert(
        self,
        participant_id: uuid.UUID,
        selector: dict[str, Any],
    ) -> dict[str, Any]:
        """Resolve a participant-bound selector and install the Saga fence."""

        candidate = self.drafts.resolve_cancel_selector(participant_id, selector)
        result = self.drafts.request_cancel(participant_id, candidate["id"])
        self._wake_runner()
        return {**self._cancel_result(result), "selector": dict(selector)}

    cancel_or_revert_import = cancel_or_revert

    def _wake_runner(self) -> None:
        notifier = self.queue_notifier
        if callable(notifier):
            try:
                notifier()
            except Exception:
                logger.exception("course_schedule_import_runner_wakeup_failed")

    def resume_cleanup_for_participant(
        self, participant_id: uuid.UUID
    ) -> int:
        resumed = self.drafts.resume_cleanup_for_participant(participant_id)
        if resumed:
            self._wake_runner()
        return resumed

    @staticmethod
    def _cancel_result(draft: dict[str, Any]) -> dict[str, Any]:
        status = str(draft.get("status") or "")
        mode = str(draft.get("cancel_mode") or "")
        if status == "cancelled":
            reply_text = (
                "这次课程表导入已撤销，相关日程已清理。"
                if mode != "before_write"
                else "已取消这次课程表导入。"
            )
        elif status == "cancelling":
            reply_text = "正在停止导入，并清理已经添加的课程…"
        elif status == "cleanup_failed":
            suffix = (
                "完成 /calendar 授权后继续。"
                if draft.get("cleanup_error_code") == "calendar_not_connected"
                else "请稍后重试清理。"
            )
            reply_text = "导入已停止，但还有部分日程尚未清理完成。" + suffix
        elif status == "expired":
            reply_text = "这份课程表导入已过期，请重新发送图片。"
        else:
            reply_text = "课程表导入状态没有改变。"
        return {
            "ok": status in {"cancelled", "cancelling", "cleanup_failed"},
            "status": status,
            "import_id": draft.get("id"),
            "cancel_mode": mode or None,
            "already_cancelled": bool(draft.get("already_cancelled")),
            "reply_text": reply_text,
        }

    def _calendar_write_enabled(self, participant_id: uuid.UUID) -> bool:
        status = self.tokens.status(participant_id)
        if not status.get("connected"):
            return False
        scopes = set(status.get("scopes") or [])
        return bool(
            "calendar:calendar.event:create" in scopes
            or "calendar:calendar" in scopes
        )

    async def _prepare_reconciliation(
        self,
        participant_id: uuid.UUID,
        draft: dict[str, Any],
        dates: set[date],
        writes_by_item: dict[str, list[CalendarWrite]],
    ) -> dict[str, Any] | None:
        repository = getattr(self.mutation_refresh, "reconciliations", None)
        if repository is None:
            return None
        today, direct, refresh, dependencies = self._mutation_work(dates)
        return await asyncio.to_thread(
            repository.create,
            participant_id,
            mutation_kind="course_schedule_import",
            direct_dates=direct,
            refresh_targets=refresh,
            dependency_sources=dependencies,
            operation={
                # This reconciliation is downstream forecast work only. The
                # course import write ledger and runner are the sole owners of
                # Calendar provider create/recovery.
                "operation_type": "course_schedule_import_forecast_refresh",
                "import_id": draft["id"],
                "source_message_id": draft["source_message_id"],
                "planner_version": COURSE_IMPORT_PLANNER_VERSION,
                "period_map_version": DEFAULT_PERIOD_MAP_VERSION,
                "recurrence_strategy": draft["recurrence_strategy"],
            },
        )

    async def _finish_reconciliation(
        self,
        reconciliation: dict[str, Any] | None,
        *,
        effect_dates: set[date],
        outcome_unknown: bool,
        outcome_unknown_error: str = "CourseScheduleBatchOutcomeUnknown",
        claim_token: uuid.UUID | str | None = None,
    ) -> dict[str, Any] | None:
        if reconciliation is None:
            return None
        repository = self.mutation_refresh.reconciliations
        claim = getattr(repository, "claim_processing", None)
        if claim_token is None and callable(claim):
            # Direct callers of this helper still get the same durable owner
            # protocol as the background runner.  This also closes the window
            # between binding the effects and a concurrent recovery scan.
            claim_token = uuid.uuid4()
            claimed = await asyncio.to_thread(
                claim,
                reconciliation["id"],
                claim_token=claim_token,
            )
            if claimed is None:
                return None
            reconciliation = claimed
        binder = getattr(repository, "bind_course_schedule_effect_dates", None)
        if not callable(binder):
            # Compatibility for injected repositories from older callers. The
            # production repository always has the atomic binding method.
            await asyncio.to_thread(repository.mark_fenced, reconciliation["id"])
            return reconciliation
        bound = await asyncio.to_thread(
            binder,
            reconciliation["id"],
            effect_dates=set(effect_dates),
            outcome_unknown=bool(outcome_unknown),
            outcome_unknown_error=outcome_unknown_error,
            claim_token=claim_token,
        )
        return bound

    async def _reconcile_forecasts(
        self,
        participant_id: uuid.UUID,
        dates: set[date],
        *,
        reconciliation: dict[str, Any] | None,
        reason: str = "course_schedule_import",
    ) -> None:
        if self.forecast_coordinator is None or self.forecast_snapshots is None:
            return
        _today, direct, refresh, dependencies = self._mutation_work(dates)
        errors: set[date] = set()
        if direct:
            try:
                await asyncio.to_thread(
                    self.forecast_snapshots.invalidate_for_calendar_mutation_dates,
                    self.forecast_coordinator.warnings,
                    participant_id,
                    direct,
                    reason=reason,
                )
            except Exception:
                logger.exception("course_schedule_batch_forecast_invalidation_failed")
                errors.update(direct)
        dependency_refresh = getattr(self.forecast_coordinator, "dependency_refresh", None)
        for target, source in dependencies.items():
            try:
                if dependency_refresh is not None:
                    await asyncio.to_thread(
                        dependency_refresh.invalidate_dependent_now,
                        participant_id,
                        source,
                        reason="previous_day_terminal_changed",
                    )
            except Exception:
                errors.add(target)
        if self.mutation_refresh is not None:
            kwargs: dict[str, Any] = {
                "reason": reason,
                "invalidation_dates": errors & direct,
                "dependency_invalidation_sources": {
                    target: source for target, source in dependencies.items() if target in errors
                },
            }
            if reconciliation is not None:
                kwargs["reconciliation_id"] = reconciliation["id"]
            self.mutation_refresh.enqueue(participant_id, refresh, **kwargs)

    async def reconcile_deleted_dates(
        self,
        participant_id: uuid.UUID,
        dates: set[date],
    ) -> None:
        """Refresh only dates whose provider effect is confirmed deleted/404."""

        await self._reconcile_forecasts(
            participant_id,
            set(dates),
            reconciliation=None,
            reason="course_schedule_import_rollback",
        )

    def _mutation_work(
        self, dates: set[date]
    ) -> tuple[date, set[date], dict[date, bool], dict[date, date]]:
        today = datetime.now(self.timezone).date()
        direct = {value for value in dates if value >= today}
        refresh = {value: True for value in sorted(direct)}
        dependencies: dict[date, date] = {}
        if today in direct:
            tomorrow = today + timedelta(days=1)
            if tomorrow not in refresh:
                refresh[tomorrow] = False
                dependencies[tomorrow] = today
        return today, direct, refresh, dependencies

    @staticmethod
    def _require_owner(draft: dict[str, Any] | None, participant_id: uuid.UUID) -> None:
        if draft is None:
            raise LookupError("draft not found")
        if draft["participant_id"] != str(participant_id):
            raise PermissionError("draft belongs to another participant")

    @staticmethod
    def _queued_result(
        draft: dict[str, Any], *, planned_writes: int | None = None
    ) -> dict[str, Any]:
        return {
            "ok": True,
            "status": draft["status"],
            "import_id": draft["id"],
            "recurrence_strategy": draft.get("recurrence_strategy"),
            "succeeded": sum(item["status"] == "succeeded" for item in draft["items"]),
            "failed": sum(item["status"] == "failed" for item in draft["items"]),
            "planned_writes": planned_writes,
            "reply_text": "正在添加课程…",
        }

    @staticmethod
    def _result(draft: dict[str, Any], *, already_completed: bool = False) -> dict[str, Any]:
        succeeded = sum(item["status"] == "succeeded" for item in draft["items"])
        failed = sum(
            item["status"] in {"failed", "running"}
            for item in draft["items"]
        )
        writes = list(draft.get("writes") or [])
        write_statuses = [str(write.get("status") or "") for write in writes]
        created_write_count = write_statuses.count("created")
        failed_write_count = write_statuses.count("create_failed")
        unknown_write_count = write_statuses.count("create_outcome_unknown")
        conflict_write_count = write_statuses.count("create_identity_conflict")
        partial_course_count = sum(
            item["status"] in {"failed", "running"}
            for item in draft["items"]
        )
        strategy_label = (
            "按课表周期规则"
            if draft.get("recurrence_strategy") == PRESERVE_SCHEDULE_PATTERN
            else "全部单次"
        )
        strategy_text = f"\n当前导入方式：{strategy_label}。"
        authorization_lost = any(
            item["status"] == "failed"
            and item.get("error_code") == "calendar_not_connected"
            for item in draft["items"]
        )
        identity_conflict = any(
            item.get("error_code") == "provider_event_identity_conflict"
            for item in draft["items"]
        )
        if draft.get("status") == "cancelled":
            text = (
                "已取消这次课程表导入。"
                if draft.get("cancel_mode") == "before_write"
                else "这次课程表导入已撤销，相关日程已清理。"
            )
        elif draft.get("status") == "cancelling":
            text = "正在停止导入，并清理已经添加的课程…"
        elif draft.get("status") == "cleanup_failed":
            text = "导入已停止，但还有部分日程尚未清理完成。"
            if draft.get("cleanup_error_code") == "calendar_not_connected":
                text += "完成 /calendar 授权后继续。"
        elif already_completed:
            text = "这份课程表已经添加过了，无需重复操作。"
        elif identity_conflict:
            text = (
                "部分日程状态需要核对，请暂时不要重复导入。"
                + strategy_text
            )
        elif authorization_lost:
            text = (
                "部分课程已经添加。\n剩余课程需要重新完成 /calendar 授权；"
                "授权后可继续重试，不会重复已经成功的课程。"
                + strategy_text
            )
        elif failed:
            if writes:
                unresolved = failed_write_count + unknown_write_count + conflict_write_count
                text = (
                    f"已确认创建 {created_write_count} 个日程；"
                    f"还有 {unresolved} 个日程未能确认，"
                    f"涉及 {partial_course_count} 门课程未完整导入。\n"
                    "你可以稍后只重试失败的内容。"
                    + strategy_text
                )
            else:
                text = (
                    f"已添加 {succeeded} 项，有 {failed} 项没能添加。\n"
                    "你可以稍后只重试失败的内容。"
                    + strategy_text
                )
        else:
            text = f"已添加 {succeeded} 项课程到日历。"
        return {
            "ok": failed == 0 and draft.get("status") not in {
                "cancelling", "cleanup_failed"
            },
            "status": draft["status"],
            "import_id": draft["id"],
            "recurrence_strategy": draft.get("recurrence_strategy"),
            "succeeded": succeeded,
            "failed": failed,
            "created_write_count": created_write_count,
            "failed_write_count": failed_write_count,
            "unknown_write_count": unknown_write_count,
            "conflict_write_count": conflict_write_count,
            "partial_course_count": partial_course_count,
            **(
                {"error": "provider_event_identity_conflict"}
                if identity_conflict
                else {"error": "calendar_not_connected"}
                if draft.get("status") == "cleanup_failed"
                and draft.get("cleanup_error_code") == "calendar_not_connected"
                else {"error": "calendar_not_connected"}
                if authorization_lost
                else {}
            ),
            "reply_text": text,
        }

    @staticmethod
    def _source_identity(
        draft: dict[str, Any],
        item: dict[str, Any],
        write: CalendarWrite,
        strategy: str,
    ) -> str:
        return (
            f"schedule:{draft['id']}:{strategy}:{item['normalized_key']}:"
            f"{write.occurrence_identity}"
        )
