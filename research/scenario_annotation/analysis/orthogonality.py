"""Evaluate every declared pair expectation without dropping missing checks."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Mapping

from ..annotation_catalog import BY_VARIABLE
from .common import AnnotationRow


@dataclass(frozen=True)
class OrthogonalityResult:
    pair_id: str
    comparison_mode: str
    annotator_id: str | None
    construct: str
    expectation: str
    left_target_ref: str | None
    right_target_ref: str | None
    left_labels: list[dict[str, Any]]
    right_labels: list[dict[str, Any]]
    status: str
    outcome: str | None
    design_issue: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _targets(scenario: Mapping[str, Any], module: str) -> list[str]:
    if module == "C":
        return [str(item.get("response_unit_ref", "")) for item in scenario.get("bot_response_units", [])]
    return list(dict.fromkeys(
        str(item.get("event_ref", ""))
        for collection in ("focal_events", "current_tasks")
        for item in scenario.get(collection, [])
        if item.get("event_ref")
    ))


def _distribution(labels: list[dict[str, Any]]) -> dict[str, float]:
    counts = Counter(repr(item["label"]) for item in labels)
    return {
        label: count / len(labels)
        for label, count in sorted(counts.items())
    }


def _outcome(expectation: str, left: list[dict[str, Any]], right: list[dict[str, Any]], mode: str) -> str:
    if mode == "BETWEEN_GROUPS":
        changed = _distribution(left) != _distribution(right)
    else:
        changed = left[0]["label"] != right[0]["label"]
    if expectation == "SENSITIVE":
        return "EXPECTED_CHANGE" if changed else "MISSING_EXPECTED_CHANGE"
    return "POTENTIAL_SPILLOVER" if changed else "INVARIANT_HELD"


def _record_labels(
    rows: list[AnnotationRow], *, scenario_id: str, target_ref: str,
    target_variable: str, record_attribute: str | None,
) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row.scenario_id != scenario_id or row.target_ref != target_ref or row.variable != target_variable:
            continue
        value = row.record.get(record_attribute) if record_attribute else row.label
        if value is None:
            continue
        result[row.annotator_id].append({"annotator_id": row.annotator_id, "label": value})
    return result


def analyze_orthogonality(
    rows: Iterable[AnnotationRow],
    pair_designs: Iterable[Mapping[str, Any]],
    *,
    scenarios: Mapping[str, Mapping[str, Any]] | None = None,
    assignment_coverage: Mapping[str, Mapping[str, set[str]]] | None = None,
) -> list[OrthogonalityResult]:
    rows = list(rows)
    all_annotators = sorted({row.annotator_id for row in rows})
    results: list[OrthogonalityResult] = []
    for pair in pair_designs:
        pair_id = str(pair.get("pair_id", ""))
        scenario_ids = [str(value) for value in pair.get("scenario_ids", [])]
        mode = str(pair.get("comparison_mode", ""))
        if len(scenario_ids) != 2:
            continue
        left_id, right_id = scenario_ids
        mapping_all = pair.get("target_mapping", {})
        for expectation, constructs in (
            ("SENSITIVE", pair.get("expected_sensitive_constructs", [])),
            ("INVARIANT", pair.get("expected_invariant_constructs", [])),
        ):
            for raw_construct in constructs:
                construct = str(raw_construct)
                mapping = mapping_all.get(construct, {}) if isinstance(mapping_all, Mapping) else {}
                variable = str(mapping.get("target_variable", construct)) if isinstance(mapping, Mapping) else construct
                record_attribute = str(mapping["record_attribute"]) if isinstance(mapping, Mapping) and mapping.get("record_attribute") else None
                spec = BY_VARIABLE.get(variable)
                issue: str | None = None
                if spec is None:
                    issue = f"unknown annotation variable {variable!r}"
                elif mode not in {"WITHIN_ANNOTATOR", "BETWEEN_GROUPS"}:
                    issue = f"unknown comparison_mode {mode!r}"

                resolved: list[str | None] = []
                for side, scenario_id, explicit in (
                    ("left", left_id, mapping.get("left_target_ref") if isinstance(mapping, Mapping) else None),
                    ("right", right_id, mapping.get("right_target_ref") if isinstance(mapping, Mapping) else None),
                ):
                    candidates: list[str] = []
                    scenario = scenarios.get(scenario_id) if scenarios else None
                    if scenario is not None and spec is not None:
                        candidates = _targets(scenario, spec.module)
                    elif spec is not None:
                        candidates = sorted({
                            row.target_ref for row in rows
                            if row.scenario_id == scenario_id and row.variable == variable
                        })
                    if explicit:
                        target_ref = str(explicit)
                        if candidates and target_ref not in candidates:
                            issue = issue or f"{side} target {target_ref!r} is not present in scenario {scenario_id}"
                        resolved.append(target_ref)
                    elif len(candidates) == 1:
                        resolved.append(candidates[0])
                    elif len(candidates) == 0:
                        if scenarios is not None:
                            issue = issue or f"no {side} target for {variable!r} in scenario {scenario_id}"
                        resolved.append(None)
                    else:
                        issue = issue or f"ambiguous {side} target for {variable!r} in scenario {scenario_id}: {candidates}"
                        resolved.append(None)

                left_ref, right_ref = resolved
                left_by_rater = _record_labels(
                    rows, scenario_id=left_id, target_ref=left_ref or "",
                    target_variable=variable, record_attribute=record_attribute,
                ) if left_ref else {}
                right_by_rater = _record_labels(
                    rows, scenario_id=right_id, target_ref=right_ref or "",
                    target_variable=variable, record_attribute=record_attribute,
                ) if right_ref else {}
                if mode == "WITHIN_ANNOTATOR":
                    if assignment_coverage is None:
                        raters: list[str | None] = all_annotators or [None]
                    else:
                        left_assigned = assignment_coverage.get(left_id, {}).get(spec.module if spec else "", set())
                        right_assigned = assignment_coverage.get(right_id, {}).get(spec.module if spec else "", set())
                        raters = sorted(left_assigned & right_assigned)
                        if not raters:
                            results.append(OrthogonalityResult(
                                pair_id, mode, None, construct, expectation,
                                left_ref, right_ref, [], [], "INVALID_DESIGN", None,
                                "WITHIN_ANNOTATOR assignment has no shared annotator",
                            ))
                            continue
                    for annotator in raters:
                        left = left_by_rater.get(annotator or "", [])
                        right = right_by_rater.get(annotator or "", [])
                        duplicate = len(left) > 1 or len(right) > 1
                        local_issue = issue or ("multiple records for a target/variable/rater" if duplicate else None)
                        status = "INVALID_DESIGN" if local_issue else "EVALUATED" if len(left) == 1 and len(right) == 1 else "NOT_COMPARABLE"
                        outcome = _outcome(expectation, left, right, mode) if status == "EVALUATED" else None
                        results.append(OrthogonalityResult(
                            pair_id, mode, annotator, construct, expectation,
                            left_ref, right_ref, left, right, status, outcome, local_issue,
                        ))
                else:
                    if assignment_coverage is None:
                        left_assigned = set(left_by_rater)
                        right_assigned = set(right_by_rater)
                    else:
                        module = spec.module if spec else ""
                        left_assigned = assignment_coverage.get(left_id, {}).get(module, set())
                        right_assigned = assignment_coverage.get(right_id, {}).get(module, set())
                    overlapping_raters = left_assigned & right_assigned
                    duplicate = any(
                        len(values) > 1
                        for annotator in left_assigned
                        for values in (left_by_rater.get(annotator, []),)
                    ) or any(
                        len(values) > 1
                        for annotator in right_assigned
                        for values in (right_by_rater.get(annotator, []),)
                    )
                    unexpected = (
                        (set(left_by_rater) - left_assigned)
                        | (set(right_by_rater) - right_assigned)
                    ) if assignment_coverage is not None else set()
                    left = [item for annotator in sorted(left_assigned) for item in left_by_rater.get(annotator, [])]
                    right = [item for annotator in sorted(right_assigned) for item in right_by_rater.get(annotator, [])]
                    has_missing_assignment = (
                        any(len(left_by_rater.get(annotator, [])) != 1 for annotator in left_assigned)
                        or any(len(right_by_rater.get(annotator, [])) != 1 for annotator in right_assigned)
                        if assignment_coverage is not None
                        else not left or not right
                    )
                    local_issue = issue or (
                        "BETWEEN_GROUPS assignment contains an empty group"
                        if assignment_coverage is not None and (not left_assigned or not right_assigned) else
                        "annotator overlap violates BETWEEN_GROUPS assignment" if overlapping_raters else
                        "annotations include annotators outside BETWEEN_GROUPS assignments" if unexpected else
                        "multiple records for a target/variable/rater" if duplicate else None
                    )
                    status = (
                        "INVALID_DESIGN" if local_issue
                        else "NOT_COMPARABLE" if has_missing_assignment
                        else "EVALUATED"
                    )
                    outcome = _outcome(expectation, left, right, mode) if status == "EVALUATED" else None
                    results.append(OrthogonalityResult(
                        pair_id, mode, None, construct, expectation,
                        left_ref, right_ref, left, right, status, outcome, local_issue,
                    ))
    return results


def orthogonality_summary(results: Iterable[OrthogonalityResult]) -> dict[str, Any]:
    rows = list(results)
    missing = [
        {
            "pair_id": row.pair_id,
            "comparison_mode": row.comparison_mode,
            "annotator_id": row.annotator_id,
            "construct": row.construct,
            "expectation": row.expectation,
            "status": row.status,
            "design_issue": row.design_issue,
        }
        for row in rows if row.status == "NOT_COMPARABLE"
    ]
    invalid_design = [
        {
            "pair_id": row.pair_id,
            "comparison_mode": row.comparison_mode,
            "annotator_id": row.annotator_id,
            "construct": row.construct,
            "expectation": row.expectation,
            "design_issue": row.design_issue,
        }
        for row in rows if row.status == "INVALID_DESIGN"
    ]
    return {
        "expected_checks": len(rows),
        "evaluated_checks": sum(row.status == "EVALUATED" for row in rows),
        "missing_checks": len(missing),
        "missing_check_details": missing,
        "invalid_design_checks": len(invalid_design),
        "invalid_design_details": invalid_design,
    }
