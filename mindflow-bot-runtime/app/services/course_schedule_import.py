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
    CalendarWriteKind,
    course_weeks,
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
    ) -> None:
        self.drafts = drafts
        self.calendar = calendar
        self.tokens = tokens
        self.timezone = ZoneInfo(timezone_name)
        self.forecast_coordinator = forecast_coordinator
        self.forecast_snapshots = forecast_snapshots
        self.mutation_refresh = mutation_refresh
        self.max_calendar_writes = max(1, int(max_calendar_writes))

    async def confirm(
        self,
        participant_id: uuid.UUID,
        import_id: uuid.UUID | str,
        *,
        recurrence_strategy: str | None = None,
    ) -> dict[str, Any]:
        draft = await asyncio.to_thread(
            self.drafts.validate_for_confirmation, participant_id, import_id
        )
        if draft["status"] == "succeeded":
            return self._result(draft, already_completed=True)
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
        all_dates: set[date] = set()
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
            for write in writes:
                all_dates.update(write.affected_dates)
        planned_writes = sum(len(writes) for writes in writes_by_item.values())
        if planned_writes > self.max_calendar_writes:
            return {
                "ok": False,
                "error": "calendar_write_limit_exceeded",
                "status": draft["status"],
                "import_id": draft["id"],
                "recurrence_strategy": strategy,
                "planned_writes": planned_writes,
                "reply_text": (
                    f"按“全部拆成单次日程”会生成 {planned_writes} 个日程，"
                    "超过当前一次导入上限。可以改用“按课表周期规则添加”，"
                    "或缩小导入范围。"
                ),
            }
        claimed = await asyncio.to_thread(
            self.drafts.begin_confirmation, participant_id, import_id
        )
        if not claimed.get("claimed"):
            if claimed["status"] == "running":
                return {
                    "ok": True,
                    "status": "running",
                    "import_id": claimed["id"],
                    "recurrence_strategy": strategy,
                    "succeeded": sum(
                        item["status"] == "succeeded" for item in claimed["items"]
                    ),
                    "failed": 0,
                    "reply_text": "这份课程表正在添加，请不要重复操作。",
                }
            return self._result(claimed, already_completed=claimed["status"] == "succeeded")

        reconciliation = await self._prepare_reconciliation(
            participant_id, claimed, all_dates, writes_by_item
        )
        any_success = False
        outcome_unknown = False
        for item in claimed["items"]:
            if item["status"] == "succeeded":
                continue
            if not await asyncio.to_thread(
                self.drafts.claim_item, import_id, item["id"]
            ):
                continue
            last_event_id: str | None = None
            try:
                for write in writes_by_item[item["id"]]:
                    await asyncio.to_thread(
                        self.drafts.renew_run_lease, import_id
                    )
                    create = (
                        self.calendar.create_recurring_event
                        if write.write_kind == CalendarWriteKind.RECURRING
                        else self.calendar.create_single_event
                    )
                    create_args = {
                        "summary": write.summary,
                        "start_time": write.start_time,
                        "end_time": write.end_time,
                        "description": write.description,
                        "source_message_id": self._source_identity(
                            claimed, item, write, strategy
                        ),
                    }
                    if write.write_kind == CalendarWriteKind.RECURRING:
                        create_args["recurrence"] = write.recurrence
                    created = await create(participant_id, **create_args)
                    last_event_id = str((created or {}).get("id") or "") or last_event_id
                    any_success = True
            except PermissionError:
                await asyncio.to_thread(
                    self.drafts.finish_item,
                    import_id,
                    item["id"],
                    error_code="calendar_not_connected",
                )
            except Exception as exc:
                outcome_unknown = outcome_unknown or type(exc).__name__.endswith(
                    "OutcomeUnknown"
                )
                await asyncio.to_thread(
                    self.drafts.finish_item,
                    import_id,
                    item["id"],
                    error_code=type(exc).__name__,
                )
                logger.warning(
                    "course_schedule_calendar_item_failed import_id=%s item_index=%s error_class=%s",
                    claimed["id"], item["item_index"], type(exc).__name__,
                )
            else:
                await asyncio.to_thread(
                    self.drafts.finish_item,
                    import_id,
                    item["id"],
                    calendar_event_id=last_event_id,
                )

        await self._finish_reconciliation(
            reconciliation, any_success=any_success, outcome_unknown=outcome_unknown
        )
        final = await asyncio.to_thread(self.drafts.finalize, import_id)
        if any_success or outcome_unknown:
            await self._reconcile_forecasts(
                participant_id, all_dates, reconciliation=reconciliation
            )
        return self._result(final)

    def cancel(
        self, participant_id: uuid.UUID, import_id: uuid.UUID | str
    ) -> dict[str, Any]:
        draft = self.drafts.cancel(participant_id, import_id)
        status = draft["status"]
        reply = {
            "cancelled": "已取消这次课程表导入。",
            "succeeded": "这份课程表已经添加到日历，不能再取消导入。",
            "expired": "这份课程表导入已过期，请重新发送图片。",
        }.get(status, "课程表导入状态没有改变。")
        return {
            "ok": status == "cancelled",
            "status": status,
            "import_id": draft["id"],
            "reply_text": reply,
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
                "operation_type": "course_schedule_batch_create",
                "import_id": draft["id"],
                "source_message_id": draft["source_message_id"],
                "planner_version": COURSE_IMPORT_PLANNER_VERSION,
                "period_map_version": DEFAULT_PERIOD_MAP_VERSION,
                "recurrence_strategy": draft["recurrence_strategy"],
                "requested": [
                    {
                        "summary": write.summary,
                        "description": write.description,
                        "start_time": write.start_time.isoformat(),
                        "end_time": write.end_time.isoformat(),
                        "recurrence": write.recurrence or "",
                        "write_kind": write.write_kind,
                        "source_message_id": self._source_identity(
                            draft,
                            item,
                            write,
                            draft["recurrence_strategy"],
                        ),
                    }
                    for item in draft["items"]
                    for write in writes_by_item.get(item["id"], [])
                ],
            },
        )

    async def _finish_reconciliation(
        self,
        reconciliation: dict[str, Any] | None,
        *,
        any_success: bool,
        outcome_unknown: bool,
    ) -> None:
        if reconciliation is None:
            return
        repository = self.mutation_refresh.reconciliations
        if outcome_unknown:
            method = repository.mark_remote_outcome_unknown
            await asyncio.to_thread(
                method, reconciliation["id"], error_class="CourseScheduleBatchOutcomeUnknown"
            )
        elif any_success:
            await asyncio.to_thread(
                repository.mark_remote_committed,
                reconciliation["id"],
                provider_result={"import_id": reconciliation["work"]["operation"]["import_id"]},
            )
        else:
            await asyncio.to_thread(
                repository.mark_remote_failed,
                reconciliation["id"],
                error_class="CourseScheduleBatchFailed",
            )

    async def _reconcile_forecasts(
        self,
        participant_id: uuid.UUID,
        dates: set[date],
        *,
        reconciliation: dict[str, Any] | None,
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
                    reason="course_schedule_import",
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
        repository = getattr(self.mutation_refresh, "reconciliations", None)
        if reconciliation is not None and repository is not None and not errors:
            await asyncio.to_thread(repository.mark_fenced, reconciliation["id"])
        if self.mutation_refresh is not None:
            kwargs: dict[str, Any] = {
                "reason": "course_schedule_import",
                "invalidation_dates": errors & direct,
                "dependency_invalidation_sources": {
                    target: source for target, source in dependencies.items() if target in errors
                },
            }
            if reconciliation is not None:
                kwargs["reconciliation_id"] = reconciliation["id"]
            self.mutation_refresh.enqueue(participant_id, refresh, **kwargs)

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
    def _result(draft: dict[str, Any], *, already_completed: bool = False) -> dict[str, Any]:
        succeeded = sum(item["status"] == "succeeded" for item in draft["items"])
        failed = sum(item["status"] == "failed" for item in draft["items"])
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
        if already_completed:
            text = "这份课程表已经添加过了，无需重复操作。"
        elif authorization_lost:
            text = (
                "部分课程已经添加。\n剩余课程需要重新完成 /calendar 授权；"
                "授权后可继续重试，不会重复已经成功的课程。"
                + strategy_text
            )
        elif failed:
            text = (
                f"已添加 {succeeded} 项，有 {failed} 项没能添加。\n"
                "你可以稍后只重试失败的内容。"
                + strategy_text
            )
        else:
            text = f"已添加 {succeeded} 项课程到日历。"
        return {
            "ok": failed == 0,
            "status": draft["status"],
            "import_id": draft["id"],
            "recurrence_strategy": draft.get("recurrence_strategy"),
            "succeeded": succeeded,
            "failed": failed,
            **({"error": "calendar_not_connected"} if authorization_lost else {}),
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


def normalize_import_item(
    draft: dict[str, Any], item: dict[str, Any], *, timezone: ZoneInfo
) -> list[CalendarWrite]:
    return plan_course_writes(
        draft,
        item,
        strategy=PRESERVE_SCHEDULE_PATTERN,
        timezone=timezone,
    )


def _weeks(rule: dict[str, Any]) -> list[int]:
    return course_weeks(rule)
