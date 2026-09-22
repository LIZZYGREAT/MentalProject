"""Project-specific representation-boundary violation audits."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Iterable, Mapping

from .common import AnnotationRow


DIRECTIONAL_APPRAISAL = {"LOW", "MEDIUM", "HIGH"}
FORBIDDEN_EFFECT_VARIABLES = {
    "Q_BS",
    "SUPPORT_EFFECT",
    "STRESS_REDUCTION",
    "HELPED_USER",
    "EFFECTIVE_SUPPORT",
    "CARE_EFFECT",
}


@dataclass(frozen=True)
class CriticalViolation:
    scenario_id: str
    annotator_id: str
    violation_type: str
    affected_fields: tuple[str, ...]
    evidence: Mapping[str, Any]
    manual_version: str
    severity: str = "CRITICAL"

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["affected_fields"] = list(self.affected_fields)
        return value


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _scenario_evidence(scenario: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    index: dict[str, Mapping[str, Any]] = {}
    for key in ("recent_context", "observed_conversation_evidence"):
        for item in scenario.get(key, []):
            if isinstance(item, Mapping) and item.get("evidence_ref"):
                index[str(item["evidence_ref"])] = item
    return index


def _event_index(scenario: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    index: dict[str, Mapping[str, Any]] = {}
    for key in ("current_tasks", "focal_events"):
        for item in scenario.get(key, []):
            if isinstance(item, Mapping) and item.get("event_ref"):
                index[str(item["event_ref"])] = item
    return index


def find_critical_violations(
    rows: Iterable[AnnotationRow],
    scenarios: Mapping[str, Mapping[str, Any]],
    coverage_tags: Mapping[str, set[str]],
) -> list[CriticalViolation]:
    rows = list(rows)
    violations: list[CriticalViolation] = []
    active_leaves: dict[tuple[str, str], list[AnnotationRow]] = defaultdict(list)

    for row in rows:
        scenario = scenarios.get(row.scenario_id, {})
        tags = coverage_tags.get(row.scenario_id, set())
        manual_version = str(row.record.get("manual_version", "UNKNOWN"))
        evidence_index = _scenario_evidence(scenario)
        cutoff = _parse_time(scenario.get("known_at_cutoff"))

        cited = [evidence_index.get(str(ref)) for ref in row.record.get("evidence_refs", [])]
        future_refs = [
            item.get("evidence_ref")
            for item in cited
            if item is not None
            and cutoff is not None
            and (known_at := _parse_time(item.get("known_at"))) is not None
            and known_at > cutoff
        ]
        if future_refs:
            violations.append(
                CriticalViolation(
                    row.scenario_id,
                    row.annotator_id,
                    "FutureKnowledgeLeakage",
                    (row.variable,),
                    {"future_evidence_refs": future_refs, "known_at_cutoff": scenario.get("known_at_cutoff")},
                    manual_version,
                )
            )

        if row.module == "B" and row.label in DIRECTIONAL_APPRAISAL:
            participant_refs = [
                item for item in cited if item is not None and item.get("speaker") == "PARTICIPANT"
            ]
            if not participant_refs or not row.record.get("evidence_span"):
                violations.append(
                    CriticalViolation(
                        row.scenario_id,
                        row.annotator_id,
                        "UnsupportedAppraisalInference",
                        (row.variable,),
                        {
                            "label": row.label,
                            "evidence_refs": list(row.record.get("evidence_refs", [])),
                            "has_evidence_span": bool(row.record.get("evidence_span")),
                        },
                        manual_version,
                    )
                )

        if row.variable in {
            "EXECUTION_EXPOSURE",
            "DEADLINE_EXPOSURE",
            "UNCERTAINTY_EXPOSURE",
            "SOCIAL_EXPOSURE",
            "RECOVERY_OCCURRENCE",
        } and row.label == "PARTIAL":
            event = _event_index(scenario).get(row.target_ref, {})
            has_interval = bool(event.get("actual_start") and event.get("actual_end"))
            has_fraction = event.get("exposure_fraction") not in (None, "UNKNOWN")
            basis = row.record.get("partial_encoding_basis")
            if has_interval and has_fraction or (has_interval and basis == "FRACTION_ONLY") or (has_fraction and basis == "ACTUAL_INTERVAL"):
                violations.append(
                    CriticalViolation(
                        row.scenario_id,
                        row.annotator_id,
                        "ExposureDoubleEncodingViolation",
                        (row.variable, "PARTIAL_ENCODING_BASIS"),
                        {"has_actual_interval": has_interval, "has_fraction": has_fraction, "basis": basis},
                        manual_version,
                    )
                )

        if row.variable == "ACTIVE_LEAF" and row.label == "YES":
            active_leaves[(row.scenario_id, row.annotator_id)].append(row)

        if "FREE_TIME" in tags and row.variable == "RECOVERY_OCCURRENCE" and row.label in {"ACTIVE", "PARTIAL"}:
            violations.append(
                CriticalViolation(
                    row.scenario_id,
                    row.annotator_id,
                    "FreeTimeAsRecoveryError",
                    (row.variable,),
                    {"coverage_tags": sorted(tags), "label": row.label},
                    manual_version,
                )
            )

        if "MISSED_NOT_OBLIGATION" in tags and row.variable == "OBLIGATION_EXISTS" and row.label == "YES":
            violations.append(
                CriticalViolation(
                    row.scenario_id,
                    row.annotator_id,
                    "MissedCourseAutomaticObligationError",
                    (row.variable,),
                    {"coverage_tags": sorted(tags), "label": row.label},
                    manual_version,
                )
            )

        if "TASK_HELP" in tags and row.variable == "SUPPORT_GATE" and row.label == "SUPPORTIVE":
            violations.append(
                CriticalViolation(
                    row.scenario_id,
                    row.annotator_id,
                    "TaskHelpAsSupportError",
                    (row.variable,),
                    {"coverage_tags": sorted(tags), "label": row.label},
                    manual_version,
                )
            )

        if row.variable in FORBIDDEN_EFFECT_VARIABLES:
            violations.append(
                CriticalViolation(
                    row.scenario_id,
                    row.annotator_id,
                    "PersonalizationAsEffectError" if row.variable == "SUPPORT_EFFECT" else "LayerLeakage",
                    (row.variable,),
                    {"forbidden_variable": row.variable},
                    manual_version,
                )
            )

    for (scenario_id, annotator_id), leaf_rows in active_leaves.items():
        if len(leaf_rows) < 2:
            continue
        events = _event_index(scenarios.get(scenario_id, {}))
        target_refs = {row.target_ref for row in leaf_rows}
        parent_child = any(events.get(ref, {}).get("parent_ref") in target_refs for ref in target_refs)
        if parent_child:
            violations.append(
                CriticalViolation(
                    scenario_id,
                    annotator_id,
                    "ParentChildObligationDoubleCount",
                    ("ACTIVE_LEAF",),
                    {"active_leaf_refs": sorted(target_refs)},
                    str(leaf_rows[0].record.get("manual_version", "UNKNOWN")),
                )
            )
    return violations


def violation_rates(violations: Iterable[CriticalViolation], rows: Iterable[AnnotationRow]) -> dict[str, dict[str, float | int]]:
    violations = list(violations)
    rows = list(rows)
    counts = Counter(item.violation_type for item in violations)
    denominator = max(1, len(rows))
    expected = {
        "UnsupportedAppraisalInferenceRate": "UnsupportedAppraisalInference",
        "LayerLeakageRate": "LayerLeakage",
        "FutureKnowledgeLeakageRate": "FutureKnowledgeLeakage",
        "ExposureDoubleEncodingViolationRate": "ExposureDoubleEncodingViolation",
        "ParentChildObligationDoubleCountRate": "ParentChildObligationDoubleCount",
        "FreeTimeAsRecoveryErrorRate": "FreeTimeAsRecoveryError",
        "MissedCourseAutomaticObligationErrorRate": "MissedCourseAutomaticObligationError",
        "TaskHelpAsSupportErrorRate": "TaskHelpAsSupportError",
        "PersonalizationAsEffectErrorRate": "PersonalizationAsEffectError",
        "HiddenMetadataLeakageRate": "HiddenMetadataLeakage",
        "InvalidEvidenceReferenceRate": "InvalidEvidenceReference",
    }
    return {
        metric_name: {
            "count": counts[violation_type],
            "rate": counts[violation_type] / denominator,
            "denominator": denominator,
        }
        for metric_name, violation_type in expected.items()
    }
