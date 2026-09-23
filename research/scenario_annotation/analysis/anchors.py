"""Calibration design-anchor audits; anchors are QA expectations, not Gold."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping

from ..annotation_catalog import BY_VARIABLE
from ..loader import load_json, load_jsonl
from .common import AnnotationRow


def assigned_annotators_by_scenario(assignments_root: str | Path) -> dict[str, set[str]]:
    """Read Module A scenario assignments and return their expected annotators."""
    root = Path(assignments_root)
    assigned: dict[str, set[str]] = defaultdict(set)
    if not root.exists():
        return {}
    for directory in sorted(path for path in root.iterdir() if path.is_dir()):
        manifest_path = directory / "manifest.json"
        if not manifest_path.exists():
            continue
        manifest = load_json(manifest_path)
        annotator = str(manifest.get("annotator_id", ""))
        module_a = directory / "module_a.jsonl"
        if not annotator or not module_a.exists():
            continue
        for scenario in load_jsonl(module_a):
            scenario_id = str(scenario.get("scenario_id", ""))
            if scenario_id:
                assigned[scenario_id].add(annotator)
    return dict(assigned)


def load_review_decisions(path: str | Path | None) -> dict[tuple[str, str], dict[str, Any]]:
    if path is None or not Path(path).exists():
        return {}
    decisions: dict[tuple[str, str], dict[str, Any]] = {}
    for row in load_jsonl(path):
        key = (str(row["anchor_id"]), str(row["annotator_id"]))
        if key in decisions:
            raise ValueError(f"duplicate anchor review decision for {key}")
        decisions[key] = row
    return decisions


def analyze_anchors(
    anchors: Iterable[Mapping[str, Any]],
    rows: Iterable[AnnotationRow],
    *,
    expected_annotators: Mapping[str, set[str]] | None = None,
    review_decisions: Mapping[tuple[str, str], Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    rows = list(rows)
    decisions = dict(review_decisions or {})
    consumed_decisions: set[tuple[str, str]] = set()
    audit: list[dict[str, Any]] = []
    anchor_ids: set[str] = set()
    for anchor in anchors:
        anchor_id = str(anchor["anchor_id"])
        if anchor_id in anchor_ids:
            raise ValueError(f"duplicate design anchor id: {anchor_id}")
        anchor_ids.add(anchor_id)
        scenario_id = str(anchor["scenario_id"])
        target_ref = str(anchor["target_ref"])
        construct = str(anchor["variable"])
        variable = str(anchor.get("target_variable", construct))
        record_attribute = anchor.get("record_attribute")
        spec = BY_VARIABLE.get(variable)
        if spec is None:
            audit.append(_invalid_row(anchor, f"unknown annotation variable {variable!r}"))
            continue
        if record_attribute and record_attribute not in {"partial_encoding_basis"}:
            audit.append(_invalid_row(anchor, f"unsupported annotation record attribute {record_attribute!r}"))
            continue

        candidates = [
            row for row in rows
            if row.scenario_id == scenario_id
            and row.module == spec.module
            and row.target_ref == target_ref
            and row.variable == variable
        ]
        if expected_annotators is None:
            annotators = sorted({row.annotator_id for row in candidates})
        else:
            annotators = sorted(expected_annotators.get(scenario_id, set()))
            unassigned = sorted({row.annotator_id for row in candidates} - set(annotators))
            annotators.extend(unassigned)

        if not annotators:
            audit.append({
                **_anchor_identity(anchor),
                "target_variable": variable,
                "record_attribute": record_attribute,
                "annotator_id": None,
                "observed_label": None,
                "intended_label": anchor["intended_label"],
                "annotation_ids": [],
                "status": "NOT_COMPARABLE",
                "review_status": "NOT_EVALUATED",
                "design_issue": "no assigned annotator or annotation record for anchor scenario",
            })
            continue

        for annotator in annotators:
            matching = [row for row in candidates if row.annotator_id == annotator]
            unassigned = expected_annotators is not None and annotator not in expected_annotators.get(scenario_id, set())
            base = {
                **_anchor_identity(anchor),
                "target_variable": variable,
                "record_attribute": record_attribute,
                "annotator_id": annotator,
                "intended_label": anchor["intended_label"],
                "annotation_ids": [row.annotation_id for row in matching],
            }
            if unassigned:
                audit.append({
                    **base,
                    "observed_label": matching[0].record.get(record_attribute) if matching and record_attribute else matching[0].label if matching else None,
                    "status": "INVALID_DESIGN",
                    "review_status": "INVALID",
                    "design_issue": "annotation exists for an annotator not assigned this scenario",
                })
                continue
            if len(matching) != 1:
                audit.append({
                    **base,
                    "observed_label": None,
                    "status": "NOT_COMPARABLE" if not matching else "INVALID_DESIGN",
                    "review_status": "NOT_EVALUATED" if not matching else "INVALID",
                    "design_issue": "assigned annotator has no matching record" if not matching else "multiple records match the anchor target",
                })
                continue

            row = matching[0]
            observed = row.record.get(record_attribute) if record_attribute else row.label
            if observed is None:
                audit.append({
                    **base,
                    "observed_label": None,
                    "status": "NOT_COMPARABLE",
                    "review_status": "NOT_EVALUATED",
                    "design_issue": f"matching record has no {record_attribute or 'label'} value",
                })
                continue
            decision_key = (anchor_id, annotator)
            if observed == anchor["intended_label"]:
                if decision_key in decisions:
                    raise ValueError(f"stale anchor review decision for matching annotation {decision_key}")
                audit.append({
                    **base,
                    "observed_label": observed,
                    "status": "MATCH",
                    "review_status": "NOT_REQUIRED",
                    "design_issue": None,
                })
                continue

            decision = decisions.get(decision_key)
            if decision:
                consumed_decisions.add(decision_key)
                audit.append({
                    **base,
                    "observed_label": observed,
                    "status": "MISMATCH_RESOLVED",
                    "review_status": "RESOLVED",
                    "review_disposition": decision["disposition"],
                    "reviewer_id": decision["reviewer_id"],
                    "review_rationale": decision["rationale"],
                    "design_issue": None,
                })
            else:
                audit.append({
                    **base,
                    "observed_label": observed,
                    "status": "MISMATCH_REVIEW_REQUIRED",
                    "review_status": "REVIEW_REQUIRED",
                    "design_issue": "observed label differs from the non-Gold calibration anchor",
                })

    stale = set(decisions) - consumed_decisions
    if stale:
        raise ValueError(f"anchor review decisions do not match an unresolved audit mismatch: {sorted(stale)}")
    return audit


def anchor_summary(audit: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    rows = list(audit)
    evaluated = {"MATCH", "MISMATCH_REVIEW_REQUIRED", "MISMATCH_RESOLVED"}
    unresolved = sum(row.get("status") == "MISMATCH_REVIEW_REQUIRED" for row in rows)
    return {
        "expected_anchors": len({str(row.get("anchor_id")) for row in rows}),
        "expected_checks": len(rows),
        "evaluated_checks": sum(row.get("status") in evaluated for row in rows),
        "missing_checks": sum(row.get("status") == "NOT_COMPARABLE" for row in rows),
        "invalid_checks": sum(row.get("status") == "INVALID_DESIGN" for row in rows),
        "matching_checks": sum(row.get("status") == "MATCH" for row in rows),
        "mismatch_checks": sum(row.get("status") in {"MISMATCH_REVIEW_REQUIRED", "MISMATCH_RESOLVED"} for row in rows),
        "unexplained_mismatches": unresolved,
    }


def _anchor_identity(anchor: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "anchor_id": str(anchor["anchor_id"]),
        "scenario_id": str(anchor["scenario_id"]),
        "target_ref": str(anchor["target_ref"]),
        "variable": str(anchor["variable"]),
        "is_gold": False,
    }


def _invalid_row(anchor: Mapping[str, Any], issue: str) -> dict[str, Any]:
    return {
        **_anchor_identity(anchor),
        "annotator_id": None,
        "observed_label": None,
        "intended_label": anchor.get("intended_label"),
        "annotation_ids": [],
        "status": "INVALID_DESIGN",
        "review_status": "INVALID",
        "design_issue": issue,
    }
