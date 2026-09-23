"""Single Stage 1 annotation field and label catalog.

This module is intentionally local to Scenario Annotation Stage 1.  It is not a
project-wide metadata registry and does not encode cross-field semantics.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class FieldRequirement(str, Enum):
    REQUIRED = "REQUIRED"
    OPTIONAL = "OPTIONAL"
    SCENARIO_CONDITIONAL = "SCENARIO_CONDITIONAL"


@dataclass(frozen=True)
class FieldSpec:
    module: str
    variable: str
    value_kind: str
    options: tuple[Any, ...] = ()
    requirement: FieldRequirement = FieldRequirement.REQUIRED
    applicable_families: tuple[str, ...] = ()
    special_options: tuple[Any, ...] = ()
    placeholder: str | None = None


LOW_MEDIUM_HIGH = ("LOW", "MEDIUM", "HIGH", "UNKNOWN", "N/A")
EXPOSURE = ("INACTIVE", "ACTIVE", "PARTIAL", "UNKNOWN", "N/A")


FIELD_SPECS: tuple[FieldSpec, ...] = (
    FieldSpec("A", "EVENT_FAMILY", "select", ("COURSE", "TASK", "STRUCTURED_EVENT", "RECOVERY_ACTIVITY", "SLEEP", "NAP", "CONSEQUENCE_EVENT", "OTHER", "UNKNOWN")),
    FieldSpec("A", "EVENT_SUBTYPE", "text", placeholder="controlled subtype, OTHER:<text>, UNKNOWN, or N/A"),
    FieldSpec("A", "SCHEDULED_START", "datetime", special_options=("UNKNOWN", "N/A")),
    FieldSpec("A", "SCHEDULED_END", "datetime", special_options=("UNKNOWN", "N/A")),
    FieldSpec("A", "ACTUAL_START", "datetime", special_options=("UNKNOWN", "N/A")),
    FieldSpec("A", "ACTUAL_END", "datetime", special_options=("UNKNOWN", "N/A")),
    FieldSpec("A", "DEADLINE", "datetime", special_options=("UNKNOWN", "N/A")),
    FieldSpec("A", "PROGRESS", "number", special_options=("UNKNOWN", "N/A")),
    FieldSpec("A", "ESTIMATED_TOTAL_EFFORT", "number", special_options=("UNKNOWN", "N/A")),
    FieldSpec("A", "REMAINING_EFFORT", "number", special_options=("UNKNOWN", "N/A", "SMALL", "MEDIUM", "LARGE")),
    FieldSpec("A", "PARENT_RELATION", "select", ("NONE", "PARENT_REF", "UNKNOWN", "N/A")),
    FieldSpec("A", "CANCELLATION", "select", ("YES", "NO", "UNKNOWN", "N/A")),
    FieldSpec("A", "LIFECYCLE", "select", ("SCHEDULED", "ATTENDED", "PARTIAL", "SKIPPED", "CANCELLED", "PLANNED", "OPEN", "IN_PROGRESS", "BLOCKED", "COMPLETED", "OVERDUE", "SUPERSEDED", "OCCURRED", "UNKNOWN")),
    FieldSpec("A", "OBLIGATION_EXISTS", "select", ("YES", "NO", "UNKNOWN")),
    FieldSpec("A", "OBLIGATION_STATUS", "select", ("OPEN", "IN_PROGRESS", "BLOCKED", "COMPLETED", "CANCELLED", "SUPERSEDED", "UNKNOWN", "N/A")),
    FieldSpec("A", "ACTIVE_LEAF", "select", ("YES", "NO", "UNKNOWN", "N/A")),
    FieldSpec("A", "D_POT", "select", LOW_MEDIUM_HIGH),
    FieldSpec("A", "U_CONTEXT", "select", LOW_MEDIUM_HIGH),
    FieldSpec("A", "D_S", "select", ("ABSENT", "PRESENT", "STRONG", "UNKNOWN", "N/A")),
    FieldSpec("A", "R_POT", "select", LOW_MEDIUM_HIGH),
    FieldSpec("A", "M_CONTEXT", "select", ("POOR", "PARTIAL", "GOOD", "UNKNOWN", "N/A")),
    FieldSpec("A", "EXECUTION_EXPOSURE", "select", EXPOSURE),
    FieldSpec("A", "DEADLINE_EXPOSURE", "select", EXPOSURE),
    FieldSpec("A", "UNCERTAINTY_EXPOSURE", "select", EXPOSURE),
    FieldSpec("A", "SOCIAL_EXPOSURE", "select", EXPOSURE),
    FieldSpec("A", "RECOVERY_OCCURRENCE", "select", EXPOSURE),
    FieldSpec("A", "DEADLINE_SCARCITY_BAND", "select", LOW_MEDIUM_HIGH, FieldRequirement.SCENARIO_CONDITIONAL),
    FieldSpec("B", "C_EXEC", "select", ("LOW", "MEDIUM", "HIGH", "NO_EVIDENCE", "AMBIGUOUS")),
    FieldSpec("B", "IMPORTANCE", "select", ("LOW", "MEDIUM", "HIGH", "NO_EVIDENCE", "AMBIGUOUS")),
    FieldSpec("B", "C_OUT", "select", ("LOW", "MEDIUM", "HIGH", "NO_EVIDENCE", "AMBIGUOUS")),
    FieldSpec("B", "U_PERC", "select", ("LOW", "MEDIUM", "HIGH", "NO_EVIDENCE", "AMBIGUOUS")),
    FieldSpec("B", "F_REC", "select", ("LOW", "MEDIUM", "HIGH", "NO_EVIDENCE", "AMBIGUOUS")),
    FieldSpec("C", "ORIGIN", "select", ("USER_INITIATED", "BOT_INITIATED", "SYSTEM_TRANSACTIONAL", "UNKNOWN")),
    FieldSpec("C", "ROLE", "select", ("ORDINARY_INFORMATION", "TASK_EXECUTION_SUPPORT", "EMOTIONAL_SUPPORT", "COPING_SUPPORT", "SENSING", "RECOVERY_SUGGESTION", "SAFETY_SUPPORT", "MIXED", "OTHER", "UNKNOWN")),
    FieldSpec("C", "SUPPORT_GATE", "select", ("SUPPORTIVE", "NON_SUPPORTIVE", "SAFETY_ONLY", "AMBIGUOUS")),
    FieldSpec("C", "VALIDATION", "select", (0, 0.5, 1)),
    FieldSpec("C", "GUIDANCE", "select", (0, 0.5, 1)),
    FieldSpec("C", "RELEVANCE", "select", (0, 0.5, 1)),
    FieldSpec("C", "PERSONALIZATION", "select", ("NONE", "CURRENT_CONTEXT", "HISTORICAL_PREFERENCE", "STABLE_PROFILE", "MIXED", "UNKNOWN")),
    FieldSpec("C", "SEEN_STATUS", "select", ("SEEN", "NOT_SEEN", "UNKNOWN", "N/A")),
)


BY_MODULE: dict[str, tuple[FieldSpec, ...]] = {
    module: tuple(spec for spec in FIELD_SPECS if spec.module == module)
    for module in ("A", "B", "C")
}
BY_VARIABLE = {spec.variable: spec for spec in FIELD_SPECS}


EVENT_SUBTYPES = {
    "COURSE": {"lecture", "lab", "seminar", "course_presentation"},
    "TASK": {"assignment", "exam_preparation", "report", "paper", "research_task", "project", "administrative"},
    "STRUCTURED_EVENT": {"meeting", "presentation", "interview", "competition", "appointment"},
    "RECOVERY_ACTIVITY": {"exercise", "leisure", "walk", "entertainment", "social_recovery", "meal_break", "short_rest"},
}

LIFECYCLES = {
    "COURSE": {"SCHEDULED", "ATTENDED", "PARTIAL", "SKIPPED", "CANCELLED", "UNKNOWN"},
    "TASK": {"PLANNED", "OPEN", "IN_PROGRESS", "BLOCKED", "COMPLETED", "CANCELLED", "OVERDUE", "SUPERSEDED", "UNKNOWN"},
    "RECOVERY_ACTIVITY": {"PLANNED", "OCCURRED", "PARTIAL", "SKIPPED", "CANCELLED", "UNKNOWN"},
    "SLEEP": {"PLANNED", "OCCURRED", "PARTIAL", "SKIPPED", "CANCELLED", "UNKNOWN"},
    "NAP": {"PLANNED", "OCCURRED", "PARTIAL", "SKIPPED", "CANCELLED", "UNKNOWN"},
    "STRUCTURED_EVENT": {"SCHEDULED", "OCCURRED", "PARTIAL", "CANCELLED", "UNKNOWN"},
    "CONSEQUENCE_EVENT": {"OCCURRED", "UNKNOWN"},
    "OTHER": {"SCHEDULED", "OCCURRED", "PARTIAL", "CANCELLED", "UNKNOWN"},
    "UNKNOWN": {"UNKNOWN"},
}


def field_specs(module: str) -> tuple[FieldSpec, ...]:
    try:
        return BY_MODULE[module]
    except KeyError as exc:
        raise ValueError(f"unknown annotation module: {module}") from exc

