"""Authoritative event classification and lifecycle finalization."""

from __future__ import annotations

from copy import deepcopy
import re
from typing import Any, Mapping

from services.course_catalog import (
    DEFAULT_COURSE_CATALOG_RESOLVER,
    CourseCandidate,
    CourseCatalogResolver,
)
from settings.event_routing import COURSE_HINT_PATTERN, ROUTINE_PATTERNS, TASK_PATTERNS


EVENT_CLASSIFICATION_VERSION = "event_classification.v3"
CLASSIFIABLE_EVENT_TYPES = {
    "course",
    "task",
    "rest",
    "meal",
    "nap",
    "sleep",
    "gym",
    "library",
    "other",
}
TASK_TYPES = {"general", "homework", "ddl", "exam", "meeting", "course"}
COMPLETION_RELEVANT_POLICIES = {"work_session", "deliverable", "progress"}

_WORK_VERBS = re.compile(
    r"完成|写|做|改|赶|准备|复习|预习|整理|制作|修改|推进|刷题|finish|write|prepare|review",
    flags=re.IGNORECASE,
)
_COURSE_TASK_WORDS = re.compile(
    r"作业|报告|论文|项目|实验|代码|算法|复习|预习|刷题|考试|测验|期末|期中|ddl|deadline|截止",
    flags=re.IGNORECASE,
)


def classify_event(
    summary: str,
    description: str,
    explicit_type: str,
    explicit_task: str,
    *,
    resolver: CourseCatalogResolver | None = None,
) -> dict[str, Any]:
    resolver = resolver or DEFAULT_COURSE_CATALOG_RESOLVER
    title = str(summary or "").strip()
    description = str(description or "").strip()
    folded_title = title.casefold()
    explicit_type = str(explicit_type or "").strip().casefold()
    explicit_task = str(explicit_task or "").strip().casefold()
    # The title owns event intent. Description text may provide context later,
    # but it cannot turn a task into a course or vice versa.
    resolution = resolver.resolve(title)

    event_type = "task"
    task_type = "general"
    event_kind = "general_task"
    source = "fallback"
    lock = ""

    if explicit_type in CLASSIFIABLE_EVENT_TYPES:
        event_type = explicit_type
        task_type = _task_type_for_explicit(explicit_type, explicit_task, folded_title)
        event_kind = _event_kind(event_type, task_type)
        source = "explicit"
        lock = "explicit"
    else:
        routine = _routine_classification(folded_title)
        if routine is not None:
            event_type, task_type, event_kind = routine
            source = "routine_rule"
            lock = "routine"
        else:
            task_rule = _task_classification(folded_title, folded_title)
            if task_rule is not None:
                event_type, task_type, event_kind = task_rule
                source = "task_rule"
                lock = "course_related_task" if resolution.likely_course else ""
            elif resolution.exact_match is not None:
                event_type, task_type, event_kind = "course", "course", "course_session"
                source = _candidate_source(resolution.exact_match)
                lock = "catalog_exact"
            elif resolution.strong_course_evidence:
                event_type, task_type, event_kind = "course", "course", "course_session"
                source = (
                    "catalog_alias"
                    if resolution.alias_match_kind else "catalog_exact"
                )
                lock = "catalog_strong_evidence"
            elif re.search(COURSE_HINT_PATTERN, folded_title):
                event_type, task_type, event_kind = "course", "course", "course_session"
                source = "course_keyword_rule"

    result: dict[str, Any] = {
        "event_type": event_type,
        "task_type": task_type,
        "event_kind": event_kind,
        "classification": {
            "schema_version": EVENT_CLASSIFICATION_VERSION,
            "source": source,
            "lock": lock,
            "event_type_locked": lock in {
                "explicit",
                "routine",
                "catalog_exact",
                "catalog_strong_evidence",
                "course_related_task",
            },
            "course_identity_locked": lock == "catalog_exact",
            "explicit_event_type": explicit_type or None,
            "explicit_task_type": explicit_task or None,
            "preliminary_event_type": event_type,
            "preliminary_task_type": task_type,
            "confidence": _classification_confidence(source),
            "course_catalog_context": resolution.context(),
        },
    }
    selected = resolution.exact_match
    if selected is None and event_type == "task" and lock == "course_related_task":
        # Task intent words are not part of course identity. A second,
        # deterministic lookup may bind an exact course (for example
        # “写线性代数作业” → “线性代数”), while ambiguous aliases such as
        # “高数” remain candidate context only.
        identity_query = _COURSE_TASK_WORDS.sub("", _WORK_VERBS.sub("", title)).strip()
        if identity_query:
            selected = resolver.resolve(identity_query).exact_match
    if selected is not None:
        result["classification"]["course_identity_locked"] = True
        _apply_course_identity(
            result,
            selected,
            source=_candidate_source(selected),
            confidence=selected.local_score,
            event_type=event_type,
            catalog_revision=resolution.catalog_revision,
        )
    return result


def finalize_event_classification(
    event: Mapping[str, Any],
    *,
    external_classification: Mapping[str, Any] | None = None,
    external_course_match: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply validated external facts without overriding stronger local facts."""

    result = deepcopy(dict(event))
    metadata = dict(result.get("metadata") or {})
    classification = dict(metadata.get("classification") or {})
    lock = str(classification.get("lock") or "")
    event_type_locked = bool(
        classification.get("event_type_locked")
        or lock in {
            "explicit",
            "routine",
            "catalog_exact",
            "catalog_strong_evidence",
            "course_related_task",
        }
    )
    course_identity_locked = bool(
        classification.get("course_identity_locked") or lock == "catalog_exact"
    )
    event_type = str(result.get("event_type") or "task").casefold()
    task_type = str(result.get("task_type") or "general").casefold()

    if external_classification and not event_type_locked:
        proposed_type = str(external_classification.get("event_type") or "").casefold()
        proposed_task = str(external_classification.get("task_type") or "general").casefold()
        if proposed_type in CLASSIFIABLE_EVENT_TYPES:
            event_type = proposed_type
            task_type = (
                proposed_task if proposed_type == "task" and proposed_task in TASK_TYPES
                else "course" if proposed_type == "course"
                else "general"
            )
            classification["source"] = "semantic_api"
            classification["confidence"] = float(
                external_classification.get("confidence") or 0.0
            )

    result["event_type"] = event_type
    result["task_type"] = task_type
    if (
        external_course_match
        and external_course_match.get("matched")
        and not course_identity_locked
    ):
        candidate = CourseCandidate(
            canonical_name=str(external_course_match["canonical_name"]),
            code=str(external_course_match.get("code") or ""),
            credits=_optional_float(external_course_match.get("credits")),
            hours=_optional_float(external_course_match.get("hours")),
            hours_per_week=_optional_float(
                external_course_match.get("hours_per_week")
            ),
            local_score=float(external_course_match.get("confidence") or 0.0),
            match_reasons=("semantic_api_candidate",),
        )
        course_context = classification.get("course_catalog_context") or {}
        _apply_course_identity(
            result,
            candidate,
            source="semantic_api_candidate",
            confidence=float(external_course_match.get("confidence") or 0.0),
            event_type=event_type,
            catalog_revision=str(course_context.get("catalog_revision") or ""),
        )

    # Course identity application updates the event metadata as well as its
    # top-level presentation fields.  Refresh this working copy so the final
    # classification write does not discard that provenance.
    metadata = dict(result.get("metadata") or metadata)
    classification["final_event_type"] = event_type
    classification["final_task_type"] = task_type
    classification["event_type_locked"] = event_type_locked
    classification["course_identity_locked"] = course_identity_locked
    metadata["classification"] = classification
    metadata["task_type"] = task_type
    result["metadata"] = metadata
    result = finalize_event_lifecycle(result)
    return result


def finalize_event_lifecycle(event: Mapping[str, Any]) -> dict[str, Any]:
    result = deepcopy(dict(event))
    metadata = dict(result.get("metadata") or {})
    lifecycle = dict(result.get("lifecycle") or metadata.get("lifecycle") or {})
    event_type = str(result.get("event_type") or "task").casefold()
    task_type = str(result.get("task_type") or "general").casefold()
    event_kind = _event_kind(event_type, task_type)
    policy = completion_policy(event_kind, task_type)
    previous_policy = str(lifecycle.get("completion_policy") or "")
    lifecycle.update(
        {
            "event_kind": event_kind,
            "completion_policy": policy,
            "requires_completion_feedback": policy in COMPLETION_RELEVANT_POLICIES,
            "default_after_end": (
                "assumed_completed"
                if policy in COMPLETION_RELEVANT_POLICIES
                else "not_applicable"
            ),
        }
    )
    if policy not in COMPLETION_RELEVANT_POLICIES:
        lifecycle.update(
            {
                "outcome_status": "not_applicable",
                "outcome_source": "policy",
                "confirmed_outcome": False,
            }
        )
    elif previous_policy not in COMPLETION_RELEVANT_POLICIES:
        lifecycle.update(
            {
                "outcome_status": "pending",
                "outcome_source": "forecast_assumption",
                "confirmed_outcome": False,
            }
        )
    result["lifecycle"] = lifecycle
    metadata["lifecycle"] = lifecycle
    metadata["task_type"] = task_type
    result["metadata"] = metadata
    return result


def completion_policy(event_kind: str, task_type: str) -> str:
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
        "other",
    }:
        return "none"
    if event_kind == "deadline" or task_type == "ddl":
        return "deliverable"
    if event_kind == "work_session":
        return "work_session"
    return "progress"


def _task_classification(title: str, combined: str) -> tuple[str, str, str] | None:
    if re.search(TASK_PATTERNS["exam"], title):
        return "task", "exam", "exam"
    has_work_verb = bool(_WORK_VERBS.search(combined))
    if has_work_verb and _COURSE_TASK_WORDS.search(combined):
        return "task", "homework", "work_session"
    if re.search(TASK_PATTERNS["ddl"], title):
        return "task", "ddl", "deadline"
    if re.search(TASK_PATTERNS["meeting"], title):
        return "task", "meeting", "meeting"
    if re.search(TASK_PATTERNS["homework"], title):
        return "task", "homework", "work_session"
    if has_work_verb:
        return "task", "general", "work_session"
    return None


def _routine_classification(title: str) -> tuple[str, str, str] | None:
    for routine_type, pattern in ROUTINE_PATTERNS.items():
        if re.search(pattern, title):
            event_type = "nap" if routine_type == "nap" else routine_type
            return event_type, "general", event_type
    return None


def _task_type_for_explicit(event_type: str, task_type: str, title: str) -> str:
    if event_type == "course":
        return "course"
    if event_type != "task":
        return "general"
    if task_type in TASK_TYPES:
        return task_type
    inferred = _task_classification(title, title)
    return inferred[1] if inferred is not None else "general"


def _event_kind(event_type: str, task_type: str) -> str:
    if event_type == "course":
        return "course_session"
    if event_type != "task":
        return event_type
    return {
        "exam": "exam",
        "ddl": "deadline",
        "meeting": "meeting",
        "homework": "work_session",
        "general": "general_task",
    }.get(task_type, "general_task")


def _apply_course_identity(
    target: dict[str, Any],
    candidate: CourseCandidate,
    *,
    source: str,
    confidence: float,
    event_type: str,
    catalog_revision: str,
) -> None:
    fields = {
        "course_match_confidence": round(float(confidence), 6),
        "course_match_source": source,
        "course_catalog_revision": catalog_revision,
    }
    if event_type == "course":
        target.pop("related_course_name", None)
        target.pop("related_course_code", None)
        fields.update(
            {
                "course_name": candidate.canonical_name,
                "course_code": candidate.code,
                "credits": candidate.credits,
                "hours": candidate.hours,
                "hours_per_week": candidate.hours_per_week,
            }
        )
    else:
        target.pop("course_name", None)
        target.pop("course_code", None)
        target.pop("credits", None)
        target.pop("hours", None)
        target.pop("hours_per_week", None)
        fields.update(
            {
                "related_course_name": candidate.canonical_name,
                "related_course_code": candidate.code,
            }
        )
    target.update(fields)
    metadata = dict(target.get("metadata") or {})
    if event_type == "course":
        metadata.pop("related_course_name", None)
        metadata.pop("related_course_code", None)
    else:
        for key in (
            "course_name",
            "course_code",
            "credits",
            "hours",
            "hours_per_week",
        ):
            metadata.pop(key, None)
    metadata.update(fields)
    target["metadata"] = metadata


def _candidate_source(candidate: CourseCandidate) -> str:
    return (
        "catalog_exact"
        if "canonical_exact" in candidate.match_reasons
        else "catalog_alias"
        if any(reason.startswith("alias:") for reason in candidate.match_reasons)
        else "catalog_fuzzy"
    )


def _classification_confidence(source: str) -> float:
    return {
        "explicit": 1.0,
        "routine_rule": 0.98,
        "catalog_exact": 0.99,
        "catalog_alias": 0.95,
        "catalog_candidates": 0.78,
        "task_rule": 0.90,
        "course_keyword_rule": 0.78,
        "fallback": 0.55,
    }.get(source, 0.65)


def _optional_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
