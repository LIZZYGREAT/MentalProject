"""Normalize calendar rows into forecast-safe event lifecycle records.

The forecast is created before an event outcome exists.  This module therefore
keeps planned semantics, completion policy, and observed outcome separate.  A
missing post-event response may become ``assumed_completed`` for future state,
but it is never treated as a confirmed training label.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta
import hashlib
import re
from typing import Any, Iterable, Mapping, Optional

from entry.class_info_data import CLASS_INFO_DICT
from settings.event_routing import COURSE_HINT_PATTERN, ROUTINE_PATTERNS, TASK_PATTERNS


EVENT_SCHEMA_VERSION = "event_instance.v2"
OUTCOME_SCHEMA_VERSION = "event_outcome.v1"

CONFIRMED_COMPLETED = {"confirmed_completed", "completed", "done"}
CONFIRMED_INCOMPLETE = {
    "confirmed_incomplete",
    "incomplete",
    "partial",
    "rescheduled",
}
COMPLETION_RELEVANT_POLICIES = {"work_session", "deliverable", "progress"}

_COURSE_ALIASES = re.compile(
    r"^(?:高数|高等数学|线代|线性代数|离散数学|概率论|大学英语|英语课)$",
    flags=re.IGNORECASE,
)
_WORK_VERBS = re.compile(
    r"完成|写|做|改|赶|准备|复习|预习|整理|制作|修改|推进|finish|write|prepare|review",
    flags=re.IGNORECASE,
)
_SUBMISSION_WORDS = re.compile(
    r"交作业|提交|上交|截止|ddl|deadline|due|答辩|汇报",
    flags=re.IGNORECASE,
)
_DATE_ISO = re.compile(r"(?<!\d)(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})日?")
_DATE_MD = re.compile(r"(?<!\d)(\d{1,2})月(\d{1,2})日")
_NEXT_WEEKDAY = re.compile(r"下周([一二三四五六日天])")
_WEEKDAY_INDEX = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}


def _text(value: Any, limit: int = 500) -> str:
    return str(value or "").strip()[:limit]


def _stable_event_id(item: Mapping[str, Any], target_date: str) -> str:
    provider_id = _text(item.get("id") or item.get("event_id") or item.get("provider_event_id"), 160)
    if provider_id:
        return provider_id
    payload = "|".join(
        (
            str(target_date),
            _text(item.get("summary") or item.get("name"), 160).casefold(),
            _text(item.get("start_time"), 32),
            _text(item.get("end_time"), 32),
            _text(item.get("description"), 500).casefold(),
        )
    )
    return "calendar_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _due_date(text: str, target_date: str) -> Optional[date]:
    base = datetime.strptime(str(target_date), "%Y-%m-%d").date()
    match = _DATE_ISO.search(text)
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return None
    match = _DATE_MD.search(text)
    if match:
        try:
            candidate = date(base.year, int(match.group(1)), int(match.group(2)))
            if candidate < base - timedelta(days=30):
                candidate = date(base.year + 1, candidate.month, candidate.day)
            return candidate
        except ValueError:
            return None
    if "后天" in text:
        return base + timedelta(days=2)
    if "明天" in text or "次日" in text:
        return base + timedelta(days=1)
    match = _NEXT_WEEKDAY.search(text)
    if match:
        weekday = _WEEKDAY_INDEX[match.group(1)]
        return base + timedelta(days=(7 - base.weekday() + weekday))
    return None


def _classify(summary: str, description: str, explicit_type: str, explicit_task: str) -> tuple[str, str, str]:
    title = summary.casefold()
    combined = f"{summary} {description}".casefold()
    explicit_type = explicit_type.casefold()
    explicit_task = explicit_task.casefold()
    if explicit_type in {"rest", "meal", "nap", "sleep", "gym", "library"}:
        return explicit_type, explicit_task or "general", explicit_type
    if explicit_type == "course":
        return "course", "course", "course_session"
    if explicit_type == "task" and explicit_task:
        kind = "work_session" if explicit_task in {"homework", "ddl", "general"} else explicit_task
        return "task", explicit_task, kind

    for routine_type, pattern in ROUTINE_PATTERNS.items():
        if re.search(pattern, title):
            mapped = "nap" if routine_type == "nap" else routine_type
            return mapped, "general", mapped

    has_work_verb = bool(_WORK_VERBS.search(combined))
    if not has_work_verb and (
        summary in CLASS_INFO_DICT
        or _COURSE_ALIASES.search(summary)
        or re.search(COURSE_HINT_PATTERN, title)
    ):
        return "course", "course", "course_session"
    if re.search(TASK_PATTERNS["exam"], title):
        return "task", "exam", "exam"
    if has_work_verb and re.search(r"作业|报告|论文|项目|实验|代码|算法|复习|课程", combined):
        return "task", "homework", "work_session"
    if re.search(TASK_PATTERNS["ddl"], title):
        return "task", "ddl", "deadline"
    if re.search(TASK_PATTERNS["meeting"], title):
        return "task", "meeting", "meeting"
    if re.search(TASK_PATTERNS["homework"], title):
        return "task", "homework", "work_session"
    return "task", "general", "work_session" if has_work_verb else "general_task"


def _completion_policy(event_kind: str, task_type: str) -> str:
    if event_kind in {
        "course_session",
        "meeting",
        "exam",
        "rest",
        "meal",
        "nap",
        "sleep",
        "gym",
        "library",
    }:
        return "none"
    if event_kind == "deadline" or task_type == "ddl":
        return "deliverable"
    if event_kind == "work_session":
        return "work_session"
    return "progress"


def prepare_event_instances(
    raw_events: Iterable[Mapping[str, Any]],
    target_date: str,
    *,
    outcome_feedback: Optional[Iterable[Mapping[str, Any]]] = None,
) -> list[dict[str, Any]]:
    """Return frozen, provider-neutral events used by prediction and bot cards."""

    prepared: list[dict[str, Any]] = []
    for raw in raw_events:
        if not isinstance(raw, Mapping):
            continue
        item = deepcopy(dict(raw))
        summary = _text(item.get("summary") or item.get("name"), 160)
        if not summary:
            continue
        description = _text(item.get("description"), 800)
        event_type, task_type, event_kind = _classify(
            summary,
            description,
            _text(item.get("event_type"), 32),
            _text(item.get("task_type") or item.get("level"), 32),
        )
        event_id = _stable_event_id(item, target_date)
        combined = f"{summary} {description}"
        due = _due_date(combined, target_date)
        policy = _completion_policy(event_kind, task_type)
        metadata = dict(item.get("metadata") or {}) if isinstance(item.get("metadata"), Mapping) else {}
        obligation = None
        if due or _SUBMISSION_WORDS.search(combined):
            obligation_seed = f"{summary.casefold()}|{due.isoformat() if due else 'unknown'}"
            obligation = {
                "obligation_id": "obligation_" + hashlib.sha256(obligation_seed.encode("utf-8")).hexdigest()[:20],
                "name": summary[:120],
                "due_at": f"{due.isoformat()}T23:59:00" if due else None,
                "source": "calendar_text_rule",
                "confidence": 0.90 if due else 0.62,
            }
        lifecycle = {
            "schema_version": EVENT_SCHEMA_VERSION,
            "event_kind": event_kind,
            "completion_policy": policy,
            "requires_completion_feedback": policy in COMPLETION_RELEVANT_POLICIES,
            "default_after_end": "assumed_completed" if policy in COMPLETION_RELEVANT_POLICIES else "not_applicable",
            "outcome_status": "pending" if policy in COMPLETION_RELEVANT_POLICIES else "not_applicable",
            "outcome_source": "forecast_assumption" if policy in COMPLETION_RELEVANT_POLICIES else "policy",
            "confirmed_outcome": False,
            "work_remaining_prior": None,
            "obligation": obligation,
        }
        metadata["lifecycle"] = lifecycle
        metadata["task_type"] = task_type
        item.update(
            {
                "id": event_id,
                "event_id": event_id,
                "date": _text(item.get("date") or target_date, 10),
                "summary": summary,
                "description": description,
                "event_type": event_type,
                "task_type": task_type,
                "metadata": metadata,
                "lifecycle": lifecycle,
            }
        )
        prepared.append(item)
    return apply_outcome_feedback(prepared, outcome_feedback or [])


def apply_outcome_feedback(
    events: Iterable[Mapping[str, Any]],
    feedback: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Apply only explicit observations; latest reported observation wins."""

    indexed: dict[str, Mapping[str, Any]] = {}
    ordered = sorted(
        (item for item in feedback if isinstance(item, Mapping)),
        key=lambda item: str(item.get("reported_at") or item.get("target_time") or ""),
    )
    for item in ordered:
        if item.get("feedback_type") != "event_completion":
            continue
        payload = item.get("payload") if isinstance(item.get("payload"), Mapping) else {}
        for key in (payload.get("event_id"), payload.get("event_name")):
            normalized = _text(key, 160).casefold()
            if normalized:
                indexed[normalized] = item

    result: list[dict[str, Any]] = []
    for raw in events:
        event = deepcopy(dict(raw))
        metadata = dict(event.get("metadata") or {})
        lifecycle = dict(metadata.get("lifecycle") or event.get("lifecycle") or {})
        match = indexed.get(_text(event.get("id") or event.get("event_id"), 160).casefold())
        match = match or indexed.get(_text(event.get("summary") or event.get("name"), 160).casefold())
        if match:
            payload = match.get("payload") or {}
            status = _text(payload.get("outcome_status"), 32).casefold()
            if not status:
                status = "confirmed_completed" if bool(payload.get("completed")) else "confirmed_incomplete"
            lifecycle.update(
                {
                    "outcome_status": status,
                    "outcome_source": "user_feedback",
                    "confirmed_outcome": True,
                    "observed_at": match.get("target_time"),
                    "reported_at": match.get("reported_at"),
                }
            )
        metadata["lifecycle"] = lifecycle
        event["metadata"] = metadata
        event["lifecycle"] = lifecycle
        result.append(event)
    return result


def apply_user_appraisals(
    events: Iterable[Mapping[str, Any]],
    feedback: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Apply explicit topic-level user appraisal to matching future events."""

    profiles = []
    for item in feedback:
        if not isinstance(item, Mapping) or item.get("feedback_type") != "event_appraisal":
            continue
        payload = item.get("payload") if isinstance(item.get("payload"), Mapping) else {}
        topic = _text(payload.get("topic"), 80).casefold()
        if topic:
            profiles.append((topic, payload, item.get("reported_at")))
    result = []
    for raw in events:
        event = deepcopy(dict(raw))
        haystack = f"{event.get('summary', '')} {event.get('description', '')}".casefold()
        matched = [profile for profile in profiles if profile[0] in haystack]
        if matched:
            _, payload, reported_at = matched[-1]
            metadata = dict(event.get("metadata") or {})
            appraisal = dict(metadata.get("appraisal") or {})
            if payload.get("threat") is not None:
                appraisal["threat"] = payload["threat"]
            if payload.get("control") is not None:
                appraisal["control"] = payload["control"]
            if payload.get("perceived_difficulty") is not None:
                appraisal["expected_effort"] = payload["perceived_difficulty"]
            dislike = payload.get("dislike")
            if dislike is not None:
                appraisal["challenge"] = max(0.0, 0.55 - 0.45 * float(dislike))
            metadata["appraisal"] = appraisal
            metadata["user_appraisal"] = {
                "topic": payload.get("topic"),
                "perceived_difficulty": payload.get("perceived_difficulty"),
                "dislike": dislike,
                "source": "explicit_user_feedback",
                "reported_at": reported_at,
            }
            event["metadata"] = metadata
        result.append(event)
    return result


def completion_relevant(event: Mapping[str, Any]) -> bool:
    lifecycle = event.get("lifecycle")
    if not isinstance(lifecycle, Mapping):
        metadata = event.get("metadata") if isinstance(event.get("metadata"), Mapping) else {}
        lifecycle = metadata.get("lifecycle") if isinstance(metadata.get("lifecycle"), Mapping) else {}
    return str(lifecycle.get("completion_policy") or "none") in COMPLETION_RELEVANT_POLICIES


def outcome_is_incomplete(lifecycle: Mapping[str, Any]) -> bool:
    return str(lifecycle.get("outcome_status") or "").casefold() in CONFIRMED_INCOMPLETE
