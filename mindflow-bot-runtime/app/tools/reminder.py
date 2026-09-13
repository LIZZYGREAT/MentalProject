"""Participant-bound reminder tools; schemas never accept identity fields."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import re
from typing import Any, Literal
from zoneinfo import ZoneInfo
import uuid

from app.agent.context import AgentContext
from app.agent.tool_registry import ToolRegistry


_CLOCK = re.compile(
    r"(?:(?<!\d)(?:[01]?\d|2[0-3])[:：][0-5]\d(?!\d)|"
    r"(?:上午|下午|晚上|中午|凌晨)?\s*\d{1,2}\s*点(?:半|\s*\d{1,2}\s*分)?|"
    r"\b\d{1,2}(?::[0-5]\d)?\s*(?:am|pm)\b)",
    re.I,
)
_DATE = re.compile(
    r"(?:今天|明天|后天|大后天|周[一二三四五六日天]|星期[一二三四五六日天]|"
    r"\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}月\d{1,2}[日号]?|"
    r"\b(?:today|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b)",
    re.I,
)
_DURATION = re.compile(
    r"(?:\d+\s*(?:分钟|小时|天)\s*后|半\s*小时\s*后|"
    r"\d+\s*(?:minutes?|hours?|days?)\s*later)",
    re.I,
)
_REMINDER_INTENT = re.compile(r"(?:提醒|叫我|通知我|remind\s+me)", re.I)
_CHINESE_TIME_NUMBER = re.compile(
    r"(?<![周期])([零〇一二两三四五六七八九十]{1,3})"
    r"(?=\s*(?:点|分钟|小时|天))"
)
_CHINESE_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3,
    "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_WEEKDAYS = {
    "一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5,
    "日": 6, "天": 6,
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}


@dataclass(frozen=True)
class ReminderTimeGrounding:
    kind: Literal["absolute", "relative"]
    local_date: date | None = None
    local_time: time | None = None
    delta: timedelta | None = None


@dataclass(frozen=True)
class ParsedReminderClock:
    hour: int
    minute: int
    daypart_source: Literal["explicit", "inherited", "none"]

    @property
    def value(self) -> time:
        return time(self.hour, self.minute)


def _chinese_number(value: str) -> int:
    if "十" not in value:
        digits = "".join(str(_CHINESE_DIGITS[char]) for char in value)
        return int(digits)
    left, right = value.split("十", 1)
    tens = _CHINESE_DIGITS[left] if left else 1
    ones = _CHINESE_DIGITS[right] if right else 0
    return tens * 10 + ones


def _normalize_time_words(value: str) -> str:
    return _CHINESE_TIME_NUMBER.sub(
        lambda match: str(_chinese_number(match.group(1))), str(value)
    )


def _clock_only_clarification(value: str) -> bool:
    candidate = _normalize_time_words(value).strip()
    candidate = re.sub(
        r"^(?:就|那就|定在|改成|时间(?:是|为)?)\s*", "", candidate
    )
    candidate = re.sub(
        r"\s*(?:吧|可以|就好|谢谢)?[。！!]?$", "", candidate
    ).strip()
    return bool(_CLOCK.fullmatch(candidate))


def has_exact_time_grounding(ctx: AgentContext) -> bool:
    current = _normalize_time_words(ctx.user_request_text)
    if _DURATION.search(current) or (
        _DATE.search(current) and _CLOCK.search(current)
    ):
        return True
    if not _clock_only_clarification(current):
        return False

    prior_user_turns = [
        str(item.text)
        for item in ctx.authorization_semantic_context
        if getattr(item, "role", None) == "user" and str(item.text).strip()
    ]
    # Production semantic context normally includes the current turn last.
    # Never use it as its own clarification predecessor.
    if prior_user_turns and prior_user_turns[-1].strip() == str(
        ctx.user_request_text
    ).strip():
        prior_user_turns.pop()
    if not prior_user_turns:
        return False
    previous = _normalize_time_words(prior_user_turns[-1])
    return bool(_REMINDER_INTENT.search(previous) and _DATE.search(previous))


def _grounding_turns(ctx: AgentContext) -> tuple[str, str | None] | None:
    current = _normalize_time_words(ctx.user_request_text)
    if _DURATION.search(current) or (_DATE.search(current) and _CLOCK.search(current)):
        return current, None
    if not _clock_only_clarification(current):
        return None
    prior_user_turns = [
        str(item.text) for item in ctx.authorization_semantic_context
        if getattr(item, "role", None) == "user" and str(item.text).strip()
    ]
    if prior_user_turns and prior_user_turns[-1].strip() == str(ctx.user_request_text).strip():
        prior_user_turns.pop()
    if not prior_user_turns:
        return None
    previous = _normalize_time_words(prior_user_turns[-1])
    if not (_REMINDER_INTENT.search(previous) and _DATE.search(previous)):
        return None
    return current, previous


def _daypart(value: str) -> str | None:
    match = re.search(r"上午|下午|晚上|中午|凌晨", value)
    return match.group(0) if match else None


def _apply_daypart(hour: int, daypart: str | None) -> int:
    if daypart in {"下午", "晚上"} and hour < 12:
        return hour + 12
    if daypart == "中午" and hour < 11:
        return hour + 12
    if daypart in {"上午", "凌晨"} and hour == 12:
        return 0
    return hour


def _parse_clock(
    text_value: str, *, inherited_daypart: str | None = None,
) -> ParsedReminderClock | None:
    colon = re.search(
        r"(?:(上午|下午|晚上|中午|凌晨)\s*)?"
        r"(?<!\d)([01]?\d|2[0-3])[:：]([0-5]\d)(?!\d)",
        text_value,
    )
    if colon:
        explicit_daypart = colon.group(1)
        chosen_daypart = explicit_daypart or inherited_daypart
        return ParsedReminderClock(
            _apply_daypart(int(colon.group(2)), chosen_daypart),
            int(colon.group(3)),
            "explicit" if explicit_daypart else "inherited" if inherited_daypart else "none",
        )
    english = re.search(r"\b(\d{1,2})(?::([0-5]\d))?\s*(am|pm)\b", text_value, re.I)
    if english:
        hour = int(english.group(1)) % 12 + (12 if english.group(3).lower() == "pm" else 0)
        return ParsedReminderClock(hour, int(english.group(2) or 0), "explicit")
    chinese = re.search(
        r"(上午|下午|晚上|中午|凌晨)?\s*(\d{1,2})\s*点(?:\s*(半|\d{1,2}\s*分?))?",
        text_value,
    )
    if chinese is None:
        return None
    explicit_daypart, raw_hour, raw_minute = chinese.groups()
    chosen_daypart = explicit_daypart or inherited_daypart
    hour = _apply_daypart(int(raw_hour), chosen_daypart)
    minute = 30 if raw_minute == "半" else int((raw_minute or "0").rstrip("分 "))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return ParsedReminderClock(
        hour, minute,
        "explicit" if explicit_daypart else "inherited" if inherited_daypart else "none",
    )


def _parse_local_date(text_value: str, reference: datetime, clock: time) -> date | None:
    lowered = text_value.casefold()
    relative_days = {
        "今天": 0, "today": 0, "明天": 1, "tomorrow": 1,
        "后天": 2, "大后天": 3,
    }
    for token, days in relative_days.items():
        if token in lowered:
            return reference.date() + timedelta(days=days)
    iso = re.search(r"(?<!\d)(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?!\d)", text_value)
    if iso:
        return date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
    month_day = re.search(r"(?<!\d)(\d{1,2})月(\d{1,2})[日号]?", text_value)
    if month_day:
        candidate = date(reference.year, int(month_day.group(1)), int(month_day.group(2)))
        if candidate < reference.date():
            candidate = date(reference.year + 1, candidate.month, candidate.day)
        return candidate
    weekday = re.search(
        r"(?:周|星期)([一二三四五六日天])|\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
        text_value, re.I,
    )
    if weekday:
        token = (weekday.group(1) or weekday.group(2)).casefold()
        days = (_WEEKDAYS[token] - reference.weekday()) % 7
        if days == 0 and clock <= reference.timetz().replace(tzinfo=None):
            days = 7
        return reference.date() + timedelta(days=days)
    return None


def resolve_reminder_time_grounding(
    ctx: AgentContext, *, now: datetime, timezone_name: str,
) -> tuple[ReminderTimeGrounding, datetime] | None:
    grounding_turns = _grounding_turns(ctx)
    if grounding_turns is None:
        return None
    current, previous = grounding_turns
    source = f"{previous or ''} {current}".strip()
    project_timezone = ZoneInfo(timezone_name)
    reference = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    local_reference = reference.astimezone(project_timezone)
    duration = re.search(r"(\d+)\s*(分钟|小时|天)\s*后", source)
    if "半小时后" in source or "半 小时 后" in source:
        delta = timedelta(minutes=30)
    elif duration:
        amount = int(duration.group(1))
        unit = duration.group(2)
        delta = timedelta(**{"分钟": {"minutes": amount}, "小时": {"hours": amount}, "天": {"days": amount}}[unit])
    else:
        english_duration = re.search(r"(\d+)\s*(minutes?|hours?|days?)\s*later", source, re.I)
        if english_duration:
            amount = int(english_duration.group(1))
            unit = english_duration.group(2).lower()
            key = "minutes" if unit.startswith("minute") else "hours" if unit.startswith("hour") else "days"
            delta = timedelta(**{key: amount})
        else:
            delta = None
    if delta is not None:
        return ReminderTimeGrounding(kind="relative", delta=delta), reference + delta
    parsed_clock = _parse_clock(
        current, inherited_daypart=_daypart(previous or ""),
    )
    if parsed_clock is None:
        return None
    clock = parsed_clock.value
    date_source = current if _DATE.search(current) else previous or current
    local_date = _parse_local_date(date_source, local_reference, clock)
    if local_date is None:
        return None
    expected = datetime.combine(local_date, clock, project_timezone)
    return ReminderTimeGrounding(
        kind="absolute", local_date=local_date, local_time=clock,
    ), expected


class ReminderTools:
    def __init__(self, reminders: Any, proactive_policy: Any, *, timezone_name: str) -> None:
        self.reminders = reminders
        self.proactive_policy = proactive_policy
        self.timezone = ZoneInfo(timezone_name)

    def register(self, registry: ToolRegistry) -> None:
        registry.register(
            "reminder_create",
            "Create a participant-owned reminder only after message and exact date-time are explicit. If time is vague, ask one clarification question instead of calling this tool.",
            {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "minLength": 1, "maxLength": 500},
                    "remind_at": {"type": "string", "format": "date-time"},
                    "recurrence_type": {"type": "string", "enum": ["none", "daily", "weekly"]},
                },
                "required": ["message", "remind_at", "recurrence_type"],
                "additionalProperties": False,
            },
            self.create, effect="internal_write", authorization_requirement="direct_request",
        )
        registry.register(
            "reminder_list", "List this participant's active reminders and recent delivery failures.",
            {"type": "object", "properties": {}, "additionalProperties": False},
            self.list, effect="read", authorization_requirement="none",
        )
        registry.register(
            "reminder_cancel", "Cancel one exact participant-owned reminder after a direct request.",
            {
                "type": "object",
                "properties": {"reminder_id": {"type": "string", "format": "uuid"}},
                "required": ["reminder_id"], "additionalProperties": False,
            },
            self.cancel, effect="internal_write", authorization_requirement="direct_request",
        )

    def create(self, ctx: AgentContext, args: dict[str, Any]) -> dict[str, Any]:
        if ctx.received_at_utc is None:
            return {
                "ok": False,
                "error": "reminder_time_grounding_unavailable",
                "detail": "The trusted ingress time is unavailable.",
            }
        resolved = resolve_reminder_time_grounding(
            ctx, now=ctx.received_at_utc, timezone_name=str(self.timezone),
        )
        if resolved is None:
            return {
                "ok": False,
                "error": "reminder_time_needs_clarification",
                "detail": "The user's own words do not contain an exact reminder time.",
            }
        raw = str(args["remind_at"])
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        submitted = datetime.fromisoformat(raw)
        if submitted.tzinfo is None:
            submitted = submitted.replace(tzinfo=self.timezone)
        grounding, remind_at = resolved
        if (
            grounding.kind == "absolute"
            and submitted.astimezone(timezone.utc)
            != remind_at.astimezone(timezone.utc)
        ):
            return {
                "ok": False,
                "error": "reminder_time_not_grounded",
                "detail": "The proposed remind_at does not match the user's grounded time.",
            }
        row = self.reminders.create(
            ctx.participant_id, message=args["message"], remind_at=remind_at,
            recurrence_type=args["recurrence_type"],
        )
        local_time = remind_at.astimezone(self.timezone)
        quiet_warning = self.proactive_policy.user_requested_quiet_hours_warning(
            ctx.participant_id, remind_at
        )
        return {
            "ok": True, "reminder": {key: value for key, value in row.items() if key not in {"participant_id", "claim_token"}},
            "quiet_hours_warning": quiet_warning,
            "confirmation": f"已设置在 {local_time.strftime('%Y-%m-%d %H:%M')} 提醒。",
        }

    def list(self, ctx: AgentContext, _args: dict[str, Any]) -> dict[str, Any]:
        reminders = []
        for row in self.reminders.list_for_user(ctx.participant_id):
            item = {
                key: value
                for key, value in row.items()
                if key not in {"participant_id", "claim_token"}
            }
            if row.get("status") == "delivery_failed":
                item["delivery_notice"] = "这条提醒未能成功送达"
            reminders.append(item)
        return {"ok": True, "reminders": reminders}

    def cancel(self, ctx: AgentContext, args: dict[str, Any]) -> dict[str, Any]:
        cancelled = self.reminders.cancel(ctx.participant_id, uuid.UUID(args["reminder_id"]))
        return {"ok": cancelled, "error": None if cancelled else "reminder_not_found"}
