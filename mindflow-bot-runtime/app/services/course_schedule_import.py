"""Confirmed, participant-scoped course schedule batch Calendar import."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import logging
from typing import Any
import uuid
from zoneinfo import ZoneInfo

from app.integrations.feishu.calendar import build_recurrence_rule
from app.repositories_course_schedule import CourseScheduleImportRepository


logger = logging.getLogger(__name__)
_WEEKDAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")


@dataclass(frozen=True)
class CalendarWrite:
    summary: str
    start_time: datetime
    end_time: datetime
    description: str
    recurrence: str | None
    occurrence_identity: str
    affected_dates: tuple[date, ...]


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
    ) -> None:
        self.drafts = drafts
        self.calendar = calendar
        self.tokens = tokens
        self.timezone = ZoneInfo(timezone_name)
        self.forecast_coordinator = forecast_coordinator
        self.forecast_snapshots = forecast_snapshots
        self.mutation_refresh = mutation_refresh

    async def confirm(
        self, participant_id: uuid.UUID, import_id: uuid.UUID | str
    ) -> dict[str, Any]:
        draft = await asyncio.to_thread(self.drafts.get, import_id)
        self._require_owner(draft, participant_id)
        if draft["status"] == "succeeded":
            return self._result(draft, already_completed=True)
        if not self._calendar_write_enabled(participant_id):
            return {
                "ok": False,
                "error": "calendar_not_connected",
                "reply_text": (
                    "还差一步日历授权。\n发送 /calendar 完成授权后，"
                    "再回来确认这份课程表即可。"
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
                    "succeeded": sum(
                        item["status"] == "succeeded" for item in claimed["items"]
                    ),
                    "failed": 0,
                    "reply_text": "这份课程表正在添加，请不要重复操作。",
                }
            return self._result(claimed, already_completed=claimed["status"] == "succeeded")

        writes_by_item: dict[str, list[CalendarWrite]] = {}
        all_dates: set[date] = set()
        for item in claimed["items"]:
            if item["status"] == "succeeded":
                continue
            writes = normalize_import_item(claimed, item, timezone=self.timezone)
            writes_by_item[item["id"]] = writes
            for write in writes:
                all_dates.update(write.affected_dates)

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
                    created = await self.calendar.create_event(
                        participant_id,
                        summary=write.summary,
                        start_time=write.start_time,
                        end_time=write.end_time,
                        description=write.description,
                        recurrence=write.recurrence,
                        source_message_id=(
                            f"schedule:{claimed['id']}:{item['normalized_key']}:"
                            f"{write.occurrence_identity}"
                        ),
                    )
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
        return {"ok": True, "status": draft["status"], "reply_text": "已取消这次课程表导入。"}

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
                "requested": [
                    {
                        "summary": write.summary,
                        "description": write.description,
                        "start_time": write.start_time.isoformat(),
                        "end_time": write.end_time.isoformat(),
                        "recurrence": write.recurrence or "",
                        "source_message_id": (
                            f"schedule:{draft['id']}:{item['normalized_key']}:"
                            f"{write.occurrence_identity}"
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
        if already_completed:
            text = "这份课程表已经添加过了，无需重复操作。"
        elif failed:
            text = f"已添加 {succeeded} 项，有 {failed} 项没能添加。\n你可以稍后只重试失败的内容。"
        else:
            text = f"已添加 {succeeded} 项课程到日历。"
        return {
            "ok": failed == 0,
            "status": draft["status"],
            "succeeded": succeeded,
            "failed": failed,
            "reply_text": text,
        }


def normalize_import_item(
    draft: dict[str, Any], item: dict[str, Any], *, timezone: ZoneInfo
) -> list[CalendarWrite]:
    if not draft.get("semester_start_date"):
        raise ValueError("semester_start_date is required")
    if item.get("weekday") is None or not item.get("start_time") or not item.get("end_time"):
        raise ValueError("course item is missing actual date or time context")
    semester_monday = date.fromisoformat(draft["semester_start_date"])
    if semester_monday.weekday() != 0:
        raise ValueError("semester_start_date must be a Monday")
    weekday = int(item["weekday"])
    rule = dict(item.get("week_rule") or {})
    weeks = _weeks(rule)
    start_clock = time.fromisoformat(item["start_time"])
    end_clock = time.fromisoformat(item["end_time"])
    description = "\n".join(
        value for value in (
            f"地点：{item['location']}" if item.get("location") else "",
            "由 MindFlow 课程表导入",
        ) if value
    )
    dates = tuple(
        semester_monday + timedelta(weeks=week - 1, days=weekday - 1)
        for week in weeks
    )
    explicit = rule.get("explicit_weeks") is not None
    odd_even = str(rule.get("odd_even") or "all")
    if explicit:
        return [
            CalendarWrite(
                summary=item["course_name"],
                start_time=datetime.combine(target, start_clock, timezone),
                end_time=datetime.combine(target, end_clock, timezone),
                description=description,
                recurrence=None,
                occurrence_identity=f"week-{week}",
                affected_dates=(target,),
            )
            for week, target in zip(weeks, dates)
        ]
    interval = 1 if odd_even == "all" else 2
    recurrence = build_recurrence_rule(
        "WEEKLY",
        interval=interval,
        weekdays=[_WEEKDAYS[weekday - 1]],
        count=len(weeks),
    )
    return [
        CalendarWrite(
            summary=item["course_name"],
            start_time=datetime.combine(dates[0], start_clock, timezone),
            end_time=datetime.combine(dates[0], end_clock, timezone),
            description=description,
            recurrence=recurrence,
            occurrence_identity=f"weeks-{weeks[0]}-{weeks[-1]}-{odd_even}",
            affected_dates=dates,
        )
    ]


def _weeks(rule: dict[str, Any]) -> list[int]:
    explicit = rule.get("explicit_weeks")
    if explicit is not None:
        result = sorted({int(value) for value in explicit})
    else:
        start = int(rule["start_week"])
        end = int(rule["end_week"])
        odd_even = str(rule.get("odd_even") or "all")
        result = [
            week for week in range(start, end + 1)
            if odd_even == "all"
            or (odd_even == "odd" and week % 2 == 1)
            or (odd_even == "even" and week % 2 == 0)
        ]
    if not result:
        raise ValueError("week rule has no occurrences")
    return result
