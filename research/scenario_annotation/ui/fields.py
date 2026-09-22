"""UI field controls derived from the frozen calibration schema vocabulary."""

from __future__ import annotations

from typing import Any

from ..drafts import MODULE_VARIABLES


OPTIONS: dict[str, list[Any]] = {
    "EVENT_FAMILY": ["COURSE", "TASK", "STRUCTURED_EVENT", "RECOVERY_ACTIVITY", "SLEEP", "NAP", "CONSEQUENCE_EVENT", "OTHER", "UNKNOWN"],
    "PARENT_RELATION": ["NONE", "PARENT_REF", "UNKNOWN", "N/A"],
    "CANCELLATION": ["YES", "NO", "UNKNOWN", "N/A"],
    "LIFECYCLE": ["SCHEDULED", "ATTENDED", "PARTIAL", "SKIPPED", "CANCELLED", "PLANNED", "OPEN", "IN_PROGRESS", "BLOCKED", "COMPLETED", "OVERDUE", "SUPERSEDED", "OCCURRED", "UNKNOWN"],
    "OBLIGATION_EXISTS": ["YES", "NO", "UNKNOWN"],
    "OBLIGATION_STATUS": ["OPEN", "IN_PROGRESS", "BLOCKED", "COMPLETED", "CANCELLED", "SUPERSEDED", "UNKNOWN", "N/A"],
    "ACTIVE_LEAF": ["YES", "NO", "UNKNOWN", "N/A"],
    "D_POT": ["LOW", "MEDIUM", "HIGH", "UNKNOWN", "N/A"],
    "U_CONTEXT": ["LOW", "MEDIUM", "HIGH", "UNKNOWN", "N/A"],
    "D_S": ["ABSENT", "PRESENT", "STRONG", "UNKNOWN", "N/A"],
    "R_POT": ["LOW", "MEDIUM", "HIGH", "UNKNOWN", "N/A"],
    "M_CONTEXT": ["POOR", "PARTIAL", "GOOD", "UNKNOWN", "N/A"],
    "EXECUTION_EXPOSURE": ["INACTIVE", "ACTIVE", "PARTIAL", "UNKNOWN", "N/A"],
    "DEADLINE_EXPOSURE": ["INACTIVE", "ACTIVE", "PARTIAL", "UNKNOWN", "N/A"],
    "UNCERTAINTY_EXPOSURE": ["INACTIVE", "ACTIVE", "PARTIAL", "UNKNOWN", "N/A"],
    "SOCIAL_EXPOSURE": ["INACTIVE", "ACTIVE", "PARTIAL", "UNKNOWN", "N/A"],
    "RECOVERY_OCCURRENCE": ["INACTIVE", "ACTIVE", "PARTIAL", "UNKNOWN", "N/A"],
    "C_EXEC": ["LOW", "MEDIUM", "HIGH", "NO_EVIDENCE", "AMBIGUOUS"],
    "IMPORTANCE": ["LOW", "MEDIUM", "HIGH", "NO_EVIDENCE", "AMBIGUOUS"],
    "C_OUT": ["LOW", "MEDIUM", "HIGH", "NO_EVIDENCE", "AMBIGUOUS"],
    "U_PERC": ["LOW", "MEDIUM", "HIGH", "NO_EVIDENCE", "AMBIGUOUS"],
    "F_REC": ["LOW", "MEDIUM", "HIGH", "NO_EVIDENCE", "AMBIGUOUS"],
    "ORIGIN": ["USER_INITIATED", "BOT_INITIATED", "SYSTEM_TRANSACTIONAL", "UNKNOWN"],
    "ROLE": ["ORDINARY_INFORMATION", "TASK_EXECUTION_SUPPORT", "EMOTIONAL_SUPPORT", "COPING_SUPPORT", "SENSING", "RECOVERY_SUGGESTION", "SAFETY_SUPPORT", "MIXED", "OTHER", "UNKNOWN"],
    "SUPPORT_GATE": ["SUPPORTIVE", "NON_SUPPORTIVE", "SAFETY_ONLY", "AMBIGUOUS"],
    "VALIDATION": [0, 0.5, 1],
    "GUIDANCE": [0, 0.5, 1],
    "RELEVANCE": [0, 0.5, 1],
    "PERSONALIZATION": ["NONE", "CURRENT_CONTEXT", "HISTORICAL_PREFERENCE", "STABLE_PROFILE", "MIXED", "UNKNOWN"],
    "SEEN_STATUS": ["SEEN", "NOT_SEEN", "UNKNOWN", "N/A"],
}

DATETIME_FIELDS = {"SCHEDULED_START", "SCHEDULED_END", "ACTUAL_START", "ACTUAL_END", "DEADLINE"}
NUMBER_FIELDS = {"PROGRESS", "ESTIMATED_TOTAL_EFFORT", "REMAINING_EFFORT"}


def module_field_specs(module: str) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    for variable in MODULE_VARIABLES[module]:
        if variable in DATETIME_FIELDS:
            specs.append({"variable": variable, "kind": "datetime", "special_options": ["UNKNOWN", "N/A"]})
        elif variable in NUMBER_FIELDS:
            specs.append({"variable": variable, "kind": "number", "special_options": ["UNKNOWN", "N/A"] + (["SMALL", "MEDIUM", "LARGE"] if variable == "REMAINING_EFFORT" else [])})
        elif variable == "EVENT_SUBTYPE":
            specs.append({"variable": variable, "kind": "text", "placeholder": "controlled subtype, OTHER:<text>, UNKNOWN, or N/A"})
        else:
            specs.append({"variable": variable, "kind": "select", "options": OPTIONS[variable]})
    return specs
