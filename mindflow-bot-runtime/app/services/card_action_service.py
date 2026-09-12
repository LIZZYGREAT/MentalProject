"""Fixed, participant-bound handlers for Feishu card actions.

Card callbacks never enter the language model.  The action allowlist and field
validation here are the authority for any state change triggered by a card.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
import hashlib
import json
import logging
import re
import uuid
from typing import Any
from zoneinfo import ZoneInfo

from app.integrations.feishu.cards import (
    card_action_result_card,
    care_intervention_result_card,
    course_schedule_context_card,
    course_schedule_item_time_card,
    course_schedule_preview_card,
    course_schedule_result_card,
    daily_checkin_card,
    external_llm_consent_card,
    external_llm_consent_details_card,
    external_llm_consent_status_card,
    morning_brief_settings_card,
    memory_center_card,
    memory_clear_all_confirmation_card,
    memory_delete_confirmation_card,
    memory_detail_card,
    today_calendar_card,
)
from app.presentation.consent_texts import external_llm_consent_declined_text
from app.presentation.feature_cards import (
    OVERVIEW_FEATURE_KEY,
    build_feature_card,
    visible_feature_keys,
)
from app.repositories import ObservationRepository
from app.services.observation_forecast_refresh import ObservationForecastRefreshService
from app.services.care_outcome_refresh import CareOutcomeRefreshService


logger = logging.getLogger(__name__)


_PERIOD_TIME_MAPPING_LINE = re.compile(
    r"^\s*(?P<first>\d{1,2})(?:\s*[-–—~]\s*(?P<last>\d{1,2}))?\s*"
    r"(?:节)?\s*(?:=|:|：)\s*(?P<start>(?:[01]?\d|2[0-3]):[0-5]\d)\s*"
    r"[-–—~]\s*(?P<end>(?:[01]?\d|2[0-3]):[0-5]\d)\s*$"
)
_CHINESE_MONTH_DAY = re.compile(
    r"^\s*(?P<month>\d{1,2})\s*月\s*(?P<day>\d{1,2})\s*日?\s*$"
)
_SLASH_DATE = re.compile(
    r"^\s*(?P<year>\d{4})\s*/\s*(?P<month>\d{1,2})\s*/\s*(?P<day>\d{1,2})\s*$"
)
_WEEKDAY_NAMES = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
_CLOCK_TIME = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


def _structured_clock(values: dict[str, Any], prefix: str) -> str:
    hour = str(values.get(f"{prefix}_hour") or "").strip()
    minute = str(values.get(f"{prefix}_minute") or "").strip()
    if not hour.isdigit() or not minute.isdigit():
        raise ValueError("请选择完整的小时和分钟")
    hour_value = int(hour)
    minute_value = int(minute)
    if not 0 <= hour_value <= 23 or not 0 <= minute_value <= 59:
        raise ValueError("请选择有效的小时和分钟")
    return f"{hour_value:02d}:{minute_value:02d}"


def _legacy_clock(value: Any) -> str:
    normalized = str(value or "").strip().replace("：", ":")
    pieces = normalized.split(":", 1)
    if len(pieces) == 2 and all(piece.isdigit() for piece in pieces):
        normalized = f"{int(pieces[0]):02d}:{int(pieces[1]):02d}"
    return normalized


def _expired_schedule_card_result(import_id: uuid.UUID | str) -> dict[str, Any]:
    reply_text = "这份课程表预览已过期，请重新发送图片后再操作。"
    return {
        "ok": True,
        "error": "course_schedule_import_expired",
        "status": "expired",
        "import_id": str(import_id),
        "reply_text": reply_text,
        "card": course_schedule_result_card(
            reply_text,
            status="expired",
            import_id=str(import_id),
            error="course_schedule_import_expired",
        ),
    }


def _boolean(value: Any, field: str) -> bool:
    normalized = str(value).strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise ValueError(f"{field} must be true or false")


def _score(value: Any, field: str) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a number") from exc
    if not 0 <= score <= 10:
        raise ValueError(f"{field} must be between 0 and 10")
    return score


def _period_time_mapping(value: Any) -> dict[int | tuple[int, int], tuple[time, time]]:
    """Parse the documented card format, without model interpretation."""

    raw = str(value or "").strip()
    if not raw or len(raw) > 1200:
        raise ValueError("period time mapping is invalid")
    mapping: dict[int | tuple[int, int], tuple[time, time]] = {}
    lines = [line.strip() for line in re.split(r"[\r\n;；]+", raw) if line.strip()]
    if not lines or len(lines) > 30:
        raise ValueError("period time mapping is invalid")
    for line in lines:
        match = _PERIOD_TIME_MAPPING_LINE.fullmatch(line)
        if match is None:
            raise ValueError(
                "节次时间格式应为 1-2节：8:00-9:35，每行一条"
            )
        first = int(match.group("first"))
        last = int(match.group("last") or first)
        if not 1 <= first <= last <= 30:
            raise ValueError("节次范围应在 1 到 30 之间")
        start = time.fromisoformat(match.group("start").zfill(5))
        end = time.fromisoformat(match.group("end").zfill(5))
        if end <= start:
            raise ValueError("节次结束时间必须晚于开始时间")
        key: int | tuple[int, int] = first if first == last else (first, last)
        if key in mapping:
            raise ValueError("同一节次范围只能填写一次")
        mapping[key] = (start, end)
    return mapping


def _semester_monday(value: Any, *, reference_date: date) -> date:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("请填写第一周周一")
    try:
        slash = _SLASH_DATE.fullmatch(raw)
        chinese = _CHINESE_MONTH_DAY.fullmatch(raw)
        if slash is not None:
            parsed = date(
                int(slash.group("year")),
                int(slash.group("month")),
                int(slash.group("day")),
            )
        elif chinese is not None:
            parsed = date(
                reference_date.year,
                int(chinese.group("month")),
                int(chinese.group("day")),
            )
        else:
            parsed = date.fromisoformat(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("日期格式应为 2026-09-07、2026/9/7 或 9月7日") from exc
    if parsed.weekday() != 0:
        monday = parsed - timedelta(days=parsed.weekday())
        raise ValueError(
            f"{parsed.month} 月 {parsed.day} 日是{_WEEKDAY_NAMES[parsed.weekday()]}，"
            f"这一周的周一是 {monday.month} 月 {monday.day} 日；"
            "请填写第一周周一"
        )
    return parsed


class CardActionService:
    def __init__(
        self,
        observations: ObservationRepository,
        calendar: Any = None,
        *,
        timezone_name: str = "Asia/Shanghai",
        daily_reviews: Any = None,
        observation_refresh: ObservationForecastRefreshService,
        care_interventions: Any = None,
        care_outcome_refresh: CareOutcomeRefreshService | None = None,
        course_schedule_imports: Any = None,
        calendar_delete_executor: Any = None,
        calendar_mutation_plan_executor: Any = None,
        feature_capabilities: Any = None,
        consent_service: Any = None,
        care_preferences: Any = None,
        memory: Any = None,
    ):
        self.observations = observations
        self.calendar = calendar
        self.timezone = ZoneInfo(timezone_name)
        self.daily_reviews = daily_reviews
        self.observation_refresh = observation_refresh
        self.care_interventions = care_interventions
        self.care_outcome_refresh = care_outcome_refresh
        self.course_schedule_imports = course_schedule_imports
        self.calendar_delete_executor = calendar_delete_executor
        self.calendar_mutation_plan_executor = calendar_mutation_plan_executor
        self.feature_keys = visible_feature_keys(feature_capabilities)
        self.consent_service = consent_service
        self.care_preferences = care_preferences
        self.memory = memory

    @staticmethod
    def _fallback_event_id(
        message_id: str,
        action_value: dict[str, Any],
        form_value: dict[str, Any],
    ) -> str:
        serialized = json.dumps(
            {
                "message_id": str(message_id),
                "action": action_value,
                "form_value": form_value,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return "card:" + hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def handle(
        self,
        participant_id: uuid.UUID,
        *,
        message_id: str,
        chat_id: str | None = None,
        callback_event_id: str | None = None,
        action_value: dict[str, Any] | None,
        form_value: dict[str, Any] | None,
    ) -> dict[str, Any]:
        action = dict(action_value or {})
        action_name = str(action.get("mindflow_action") or "")
        if action_name in {
            "memory_delete_prompt", "memory_delete_confirm",
            "memory_clear_prompt", "memory_clear_confirm",
            "memory_center_refresh",
            "memory_detail_open",
        }:
            if str(action.get("version") or "") != "1":
                return {"ok": False, "error": "unsupported_card_action_version"}
            if self.memory is None:
                raise RuntimeError("Memory Center is unavailable")
            memories = self.memory.list(participant_id)
            if action_name == "memory_center_refresh":
                return {"ok": True, "reply_text": "已返回 Memory Center。", "card": memory_center_card(memories)}
            if action_name == "memory_clear_prompt":
                return {"ok": True, "reply_text": "请确认是否清空全部长期记忆。", "card": memory_clear_all_confirmation_card()}
            if action_name == "memory_clear_confirm":
                deleted = self.memory.clear_all(participant_id)
                return {"ok": True, "reply_text": f"已清空 {deleted} 条长期记忆。", "card": memory_center_card([])}
            try:
                memory_id = uuid.UUID(str(action.get("memory_id") or ""))
            except ValueError:
                return {"ok": False, "error": "invalid_memory_id"}
            target = next((item for item in memories if item["id"] == str(memory_id)), None)
            if target is None:
                return {"ok": False, "error": "memory_not_found"}
            if action_name == "memory_detail_open":
                return {"ok": True, "reply_text": "已打开记忆详情。", "card": memory_detail_card(target)}
            if action_name == "memory_delete_prompt":
                return {"ok": True, "reply_text": "请确认是否删除这条记忆。", "card": memory_delete_confirmation_card(target)}
            deleted = self.memory.delete(participant_id, memory_id)
            remaining = self.memory.list(participant_id)
            return {"ok": deleted, "reply_text": "这条记忆已删除。" if deleted else "没有找到这条记忆。", "card": memory_center_card(remaining)}
        if action_name in {
            "morning_brief_toggle", "morning_brief_time_update",
            "morning_brief_pause_week",
        }:
            if str(action.get("version") or "") != "1":
                return {"ok": False, "error": "unsupported_card_action_version"}
            if self.care_preferences is None:
                raise RuntimeError("morning brief settings are unavailable")
            changes: dict[str, Any]
            if action_name == "morning_brief_toggle":
                changes = {"morning_brief_enabled": _boolean(action.get("enabled"), "enabled")}
            elif action_name == "morning_brief_pause_week":
                changes = {
                    "morning_brief_paused_until": (
                        datetime.now(self.timezone) + timedelta(days=7)
                    ).isoformat()
                }
            else:
                selected = str((form_value or {}).get("morning_brief_local_time") or "")
                if selected not in {"07:00", "07:30", "08:00", "08:30", "09:00"}:
                    return {"ok": False, "error": "invalid_morning_brief_time"}
                changes = {"morning_brief_local_time": selected}
            updated = self.care_preferences.update(participant_id, changes)
            return {
                "ok": True,
                "reply_text": "早报设置已更新。",
                "card": morning_brief_settings_card(updated),
            }
        if action_name in {
            "course_schedule_item_time_open",
            "course_schedule_item_time_submit",
        }:
            action_version = str(action.get("version") or "")
            if action_version not in {"1", "2"}:
                return {"ok": False, "error": "unsupported_card_action_version"}
            if self.course_schedule_imports is None:
                raise RuntimeError("course schedule import service is unavailable")
            try:
                import_id = uuid.UUID(str(action.get("import_id") or ""))
                item_id = uuid.UUID(str(action.get("item_id") or ""))
            except (TypeError, ValueError) as exc:
                raise ValueError("course schedule item target is invalid") from exc
            drafts = self.course_schedule_imports.drafts
            draft = drafts.get(import_id)
            if draft is None or str(draft.get("participant_id")) != str(participant_id):
                return {"ok": False, "error": "course_schedule_import_not_found"}
            if action_name.endswith("_open"):
                return {
                    "ok": True,
                    "reply_text": "请修改这门课程的起止时间。",
                    "card": course_schedule_item_time_card(draft, str(item_id)),
                }
            values = dict(form_value or {})
            try:
                if action_version == "2":
                    start_value = _structured_clock(values, "start")
                    end_value = _structured_clock(values, "end")
                else:
                    start_value = _legacy_clock(values.get("start_time"))
                    end_value = _legacy_clock(values.get("end_time"))
            except ValueError as exc:
                return {
                    "ok": True,
                    "reply_text": str(exc),
                    "card": course_schedule_item_time_card(draft, str(item_id)),
                }
            if not _CLOCK_TIME.fullmatch(start_value) or not _CLOCK_TIME.fullmatch(
                end_value
            ):
                return {
                    "ok": True,
                    "reply_text": "请选择完整、有效的开始和结束时间。",
                    "card": course_schedule_item_time_card(draft, str(item_id)),
                }
            try:
                corrected = drafts.apply_correction(
                    participant_id,
                    import_id,
                    item_id=item_id,
                    start_time=start_value,
                    end_time=end_value,
                )
            except (LookupError, PermissionError):
                return {"ok": False, "error": "course_schedule_import_not_found"}
            except ValueError as exc:
                return {
                    "ok": True,
                    "reply_text": f"课程时间需要调整：{str(exc)[:120]}",
                    "card": course_schedule_item_time_card(draft, str(item_id)),
                }
            return {
                "ok": True,
                "status": corrected.get("status"),
                "reply_text": "课程时间已修改，请核对更新后的预览。",
                "card": course_schedule_preview_card(corrected),
            }
        if action_name in {
            "calendar_mutation_plan_confirm",
            "calendar_mutation_plan_cancel",
        }:
            if str(action.get("version") or "") != "1":
                return {"ok": False, "error": "unsupported_card_action_version"}
            if self.calendar_mutation_plan_executor is None:
                raise RuntimeError("calendar mutation plan executor is unavailable")
            try:
                plan_id = str(uuid.UUID(str(action.get("plan_id") or "")))
            except (TypeError, ValueError) as exc:
                raise ValueError("calendar mutation plan target is invalid") from exc
            import asyncio

            result = asyncio.run(
                self.calendar_mutation_plan_executor(
                    participant_id,
                    plan_id,
                    confirmed=action_name.endswith("_confirm"),
                    source_message_id=(
                        str(callback_event_id or "").strip()
                        or self._fallback_event_id(
                            message_id, action, dict(form_value or {})
                        )
                    ),
                    status_card_message_id=message_id,
                    status_card_chat_id=chat_id,
                )
            )
            if not result.get("ok"):
                return result
            reply_text = str(result.get("reply_text") or "操作已处理。")
            return {
                **result,
                "reply_text": reply_text,
                "card": card_action_result_card(message=reply_text),
            }
        if action_name in {"calendar_delete_confirm", "calendar_delete_cancel"}:
            if str(action.get("version") or "") != "1":
                return {"ok": False, "error": "unsupported_card_action_version"}
            if action_name == "calendar_delete_cancel":
                reply_text = "已取消删除，日程未更改。"
                return {
                    "ok": True,
                    "reply_text": reply_text,
                    "card": card_action_result_card(message=reply_text),
                }
            if self.calendar_delete_executor is None:
                raise RuntimeError("calendar delete executor is unavailable")
            event_id = str(action.get("event_id") or "").strip()
            if not event_id or len(event_id) > 256:
                raise ValueError("calendar event id is invalid")
            import asyncio

            result = asyncio.run(
                self.calendar_delete_executor(
                    participant_id,
                    event_id,
                    source_message_id=(
                        str(callback_event_id or "").strip()
                        or self._fallback_event_id(
                            message_id, action, dict(form_value or {})
                        )
                    ),
                )
            )
            if not result.get("ok"):
                return result
            reply_text = "日程已删除。"
            return {
                **result,
                "reply_text": reply_text,
                "card": card_action_result_card(message=reply_text),
            }
        if action_name in {
            "course_schedule_import_confirm",
            "course_schedule_import_cancel",
            "course_schedule_import_context_open",
            "course_schedule_import_context_submit",
        }:
            if self.course_schedule_imports is None:
                raise RuntimeError("course schedule import service is unavailable")
            context_action = action_name.startswith("course_schedule_import_context_")
            expected_version = "3" if context_action else "2"
            if str(action.get("version") or "") != expected_version:
                return {"ok": False, "error": "unsupported_card_action_version"}
            try:
                import_id = uuid.UUID(str(action.get("import_id") or ""))
            except (TypeError, ValueError) as exc:
                raise ValueError("course schedule import id is invalid") from exc
            if context_action:
                drafts = self.course_schedule_imports.drafts
                if action_name.endswith("_open"):
                    draft = drafts.get(import_id)
                    if draft is None or str(draft.get("participant_id")) != str(participant_id):
                        return {"ok": False, "error": "course_schedule_import_not_found"}
                    if str(draft.get("status") or "") == "expired":
                        return _expired_schedule_card_result(import_id)
                    if (
                        str(draft.get("status") or "") != "pending_context"
                        or draft.get("recurrence_strategy")
                    ):
                        return {
                            "ok": True,
                            "reply_text": "这份课表已不需要补充信息。",
                            "card": course_schedule_preview_card(draft),
                        }
                    return {
                        "ok": True,
                        "reply_text": "请补充课表导入信息。",
                        "card": course_schedule_context_card(draft),
                    }
                values = dict(form_value or {})
                try:
                    semester_value = str(values.get("semester_start_date") or "").strip()
                    semester_start_date = (
                        _semester_monday(
                            semester_value,
                            reference_date=datetime.now(self.timezone).date(),
                        )
                        if semester_value
                        else None
                    )
                    mapping_value = values.get("period_time_mapping")
                    period_mapping = (
                        _period_time_mapping(mapping_value)
                        if str(mapping_value or "").strip() else None
                    )
                    draft = drafts.apply_context_update(
                        participant_id,
                        import_id,
                        semester_start_date=semester_start_date,
                        period_time_mapping=period_mapping,
                    )
                except (LookupError, PermissionError):
                    # Do not disclose draft details when a copied/replayed card
                    # belongs to a different participant or has expired.
                    return {"ok": False, "error": "course_schedule_import_not_found"}
                except ValueError as exc:
                    if "expired" in str(exc).lower():
                        return _expired_schedule_card_result(import_id)
                    return {
                        "ok": True,
                        "reply_text": f"信息格式需要调整：{str(exc)[:120]}",
                        "card": course_schedule_context_card(
                            drafts.get(import_id) or {"id": str(import_id)}
                        ),
                    }
                missing = list(
                    dict(draft.get("structured_result") or {}).get("missing_context")
                    or []
                )
                return {
                    "ok": True,
                    "status": draft.get("status"),
                    "reply_text": (
                        "课表信息已补齐，请确认预览并选择添加方式。"
                        if not missing
                        else "已保存部分信息，还需要补充课表中的其余项目。"
                    ),
                    "card": course_schedule_preview_card(draft),
                }
            if action_name.endswith("_cancel"):
                result = self.course_schedule_imports.cancel(
                    participant_id,
                    import_id,
                    status_card_chat_id=chat_id,
                )
            else:
                import asyncio

                recurrence_strategy = str(
                    action.get("recurrence_strategy") or ""
                ).strip()
                if recurrence_strategy not in {
                    "preserve_schedule_pattern", "expand_all_occurrences"
                }:
                    raise ValueError("course recurrence strategy is invalid")

                result = asyncio.run(
                    self.course_schedule_imports.confirm(
                        participant_id,
                        import_id,
                        recurrence_strategy=recurrence_strategy,
                        status_card_message_id=message_id,
                        status_card_chat_id=chat_id,
                    )
                )
            reply_text = str(result.get("reply_text") or "课程表操作已处理。")
            # Missing Calendar authorization is a valid, non-mutating outcome.
            return {
                **result,
                "ok": True,
                "card": course_schedule_result_card(
                    reply_text,
                    status=str(result.get("status") or "") or None,
                    import_id=str(result.get("import_id") or import_id),
                    error=str(result.get("error") or "") or None,
                    recurrence_strategy=(
                        str(result.get("recurrence_strategy") or "") or None
                    ),
                ),
            }
        if action_name.startswith("care_"):
            if self.care_interventions is None:
                raise RuntimeError("care intervention service is unavailable")
            if str(action.get("version") or "") != "1":
                return {"ok": False, "error": "unsupported_card_action_version"}
            try:
                intervention_id = uuid.UUID(str(action.get("intervention_id") or ""))
            except (TypeError, ValueError) as exc:
                raise ValueError("care intervention id is invalid") from exc
            event_id = str(callback_event_id or "").strip() or self._fallback_event_id(
                message_id, action, dict(form_value or {})
            )
            care_action = action_name.removeprefix("care_")
            result = self.care_interventions.apply_action(
                participant_id,
                intervention_id,
                action=care_action,
                callback_event_id=event_id,
            )
            reply = {
                "ack": "知道了，本次提醒已确认。",
                "helpful": "谢谢反馈，我已记录这条提醒有帮助。",
                "not_relevant": "谢谢反馈，我已记录这条提醒不太相关。",
                "mute_today": "已关闭今天剩余的主动关怀提醒。",
                "snooze_30": (
                    "已安排 30 分钟后再提醒。"
                    if result.get("action_result") == "scheduled"
                    else "已记录延后选择，但受当前提醒上限或设置限制，未新增提醒。"
                ),
                "disable_type": "已关闭这一类主动关怀提醒；显式选择会优先于自动学习。",
            }.get(care_action, "关怀反馈已记录。")
            displayed_action = str(result.get("recorded_action") or care_action)
            result_text = {
                "ack": "✓ 已记录：知道了",
                "helpful": "✓ 已记录：有帮助",
                "not_relevant": "✓ 已记录：不太相关",
                "mute_today": "✓ 已记录：今天不再提醒",
                "snooze_30": (
                    "✓ 已记录：30 分钟后提醒"
                    if result.get("action_result") == "scheduled"
                    else "✓ 已记录：延后选择（未新增提醒）"
                ),
                "disable_type": "✓ 已记录：不想收到这类提醒",
            }.get(displayed_action, "✓ 关怀反馈已记录")
            intervention = result.get("intervention") or {}
            return {
                "ok": True,
                "care_intervention_id": str(intervention_id),
                "created": bool(result["created"]),
                "action_result": result.get("action_result"),
                "reply_text": reply,
                "card": care_intervention_result_card(
                    message=str(intervention.get("message") or "关怀提醒"),
                    result_text=result_text,
                ),
            }
        if action_name in {"feature_open", "feature_back"}:
            # Feature cards are navigation only; the callback carries just the
            # backend-validated feature key and version, and the response
            # updates the source card in place instead of sending a new one.
            if str(action.get("version") or "") != "1":
                return {"ok": False, "error": "unsupported_card_action_version"}
            wanted = (
                OVERVIEW_FEATURE_KEY
                if action_name == "feature_back"
                else str(action.get("feature_key") or "")
            )
            card = build_feature_card(wanted, self.feature_keys)
            if card is None:
                return {"ok": False, "error": "unsupported_feature"}
            reply_text = (
                "这些是我目前能帮你做的事情。"
                if wanted == OVERVIEW_FEATURE_KEY
                else "这一项的用法如下，随时可以直接对我说。"
            )
            # Navigation only: nothing is committed, so a failed in-place
            # update must not be reported as a recorded operation.
            return {
                "ok": True,
                "navigation_only": True,
                "reply_text": reply_text,
                "card": card,
            }
        if action_name.startswith("external_llm_consent_"):
            # User-owned external LLM consent. The callback binds to the
            # clicking participant only; grant/revoke are the fixed backend
            # workflow and the consent record is the sole authority.
            if str(action.get("version") or "") != "1":
                return {"ok": False, "error": "unsupported_card_action_version"}
            if self.consent_service is None:
                raise RuntimeError("consent service is unavailable")
            if action_name == "external_llm_consent_accept":
                # The card must carry the disclosure version the user actually
                # saw. A stale card never grants a newer consent: zero writes,
                # re-render the current disclosure instead.
                from app.services.consent_service import EXTERNAL_LLM_CONSENT_VERSION

                if str(action.get("consent_version") or "") != (
                    EXTERNAL_LLM_CONSENT_VERSION
                ):
                    return {
                        "ok": True,
                        "navigation_only": True,
                        "reply_text": (
                            "外部 AI 处理说明已经更新，请先查看最新说明后再选择。"
                        ),
                        "card": external_llm_consent_card(),
                    }
                self.consent_service.grant_external_llm_consent(participant_id)
                status = self.consent_service.status(participant_id)
                return {
                    "ok": True,
                    "reply_text": (
                        "已开启外部 AI 处理。请重新发送图片，我会直接处理；"
                        "对话也会正常回复。"
                    ),
                    "card": external_llm_consent_status_card(status),
                }
            if action_name == "external_llm_consent_decline":
                status = self.consent_service.status(participant_id)
                return {
                    "ok": True,
                    "navigation_only": True,
                    "reply_text": external_llm_consent_declined_text(),
                    "card": external_llm_consent_status_card(status),
                }
            if action_name == "external_llm_consent_revoke":
                self.consent_service.revoke_external_llm_consent(participant_id)
                status = self.consent_service.status(participant_id)
                return {
                    "ok": True,
                    "reply_text": (
                        "已关闭外部 AI 处理。已保存的记录、本地日历和压力功能不受影响。"
                    ),
                    "card": external_llm_consent_status_card(status),
                }
            if action_name == "external_llm_consent_status_open":
                status = self.consent_service.status(participant_id)
                return {
                    "ok": True,
                    "navigation_only": True,
                    "reply_text": "这是外部 AI 处理的当前状态。",
                    "card": external_llm_consent_status_card(status),
                }
            if action_name == "external_llm_consent_details_open":
                return {
                    "ok": True,
                    "navigation_only": True,
                    "reply_text": "这是外部 AI 处理的数据范围。",
                    "card": external_llm_consent_details_card(),
                }
            if action_name == "external_llm_consent_prompt_open":
                return {
                    "ok": True,
                    "navigation_only": True,
                    "reply_text": "开启后即可使用图片识别和对话处理。",
                    "card": external_llm_consent_card(),
                }
            return {"ok": False, "error": "unsupported_card_action"}
        if action_name == "request_checkin":
            return {
                "ok": True,
                "reply_text": "请填写此刻状态。",
                "card": daily_checkin_card(),
            }
        if action_name in {"view_today_calendar", "view_calendar_date"}:
            if self.calendar is None:
                raise RuntimeError("calendar service is unavailable")
            import asyncio

            today = datetime.now(self.timezone).date()
            requested_date = (
                date.fromisoformat(str(action.get("local_date") or ""))
                if action_name == "view_calendar_date"
                else today
            )
            start = datetime.combine(requested_date, time.min, self.timezone)
            events = asyncio.run(
                self.calendar.get_events(participant_id, start, start + timedelta(days=1))
            )
            requested_date_is_today = requested_date == today
            return {
                "ok": True,
                "reply_text": (
                    "已加载今日日程。"
                    if requested_date_is_today
                    else f"已加载 {requested_date.isoformat()} 的日程。"
                ),
                "card": today_calendar_card(
                    events,
                    local_date=requested_date.isoformat(),
                    requested_date_is_today=requested_date_is_today,
                ),
            }
        if action_name == "daily_review_submit":
            if self.daily_reviews is None:
                raise RuntimeError("daily review service is unavailable")
            values = dict(form_value or {})
            event_id = str(callback_event_id or "").strip() or self._fallback_event_id(
                message_id, action, values
            )
            result = self.daily_reviews.submit(
                participant_id,
                callback_event_id=event_id,
                action=action,
                values=values,
            )
            response = result["response"]
            return {
                "ok": True,
                "daily_review_response_id": response["id"],
                "reply_text": (
                    "每日回顾已记录并生成回顾估计。"
                    if result["created"] else "这次每日回顾已记录，无需重复提交。"
                ),
            }
        if (
            action_name != "submit_checkin"
            or str(action.get("version") or "") != "1"
        ):
            return {"ok": False, "error": "unsupported_card_action"}
        values = dict(form_value or {})
        stress = _score(values.get("stress"), "stress")
        energy = _score(values.get("energy"), "energy")
        activity = str(values.get("activity") or "").strip()
        if not 1 <= len(activity) <= 120:
            raise ValueError("activity must be 1-120 characters")
        stress_event = _boolean(
            values.get("stress_event_since_last"), "stress_event_since_last"
        )
        ongoing = _boolean(values.get("event_ongoing"), "event_ongoing")
        event_id = str(callback_event_id or "").strip() or self._fallback_event_id(
            message_id, action, values
        )
        write = self.observations.add_with_status(
            participant_id,
            "checkin",
            {
                "stress_0_10": stress,
                "energy_0_10": energy,
                "activity": activity,
                "stress_event_since_last": stress_event,
                "event_ongoing": ongoing,
                "input_method": "feishu_card",
                "card_message_id": str(message_id)[:128],
            },
            source_message_id=event_id[:128],
        )
        if not write.idempotency_conflict and self.care_outcome_refresh is not None:
            try:
                self.care_outcome_refresh.on_observation_committed(
                    participant_id, write.observation_id
                )
            except Exception:
                logger.exception(
                    "care outcome refresh failed after card check-in commit",
                    extra={"observation_id": str(write.observation_id)},
                )
        self.observation_refresh.on_observation_committed(
            participant_id=participant_id,
            observed_at=write.observed_at,
            created=write.created,
        )
        persisted = dict(write.persisted_payload)
        if write.idempotency_conflict:
            return {
                "ok": False,
                "error": "idempotency_conflict",
                "observation_id": str(write.observation_id),
                "reply_text": "这次提交标识已用于另一组状态数据，请重新打开卡片提交。",
            }
        return {
            "ok": True,
            "observation_id": str(write.observation_id),
            "created": write.created,
            "reply_text": (
                "已记录这次状态：压力 "
                f"{float(persisted['stress_0_10']):g}/10，精力 "
                f"{float(persisted['energy_0_10']):g}/10。"
            ),
        }
