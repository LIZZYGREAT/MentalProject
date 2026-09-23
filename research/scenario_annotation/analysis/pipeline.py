"""Validated Stage 1 analysis pipeline and artifact writers."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from ..analysis_manifest import build_analysis_manifest, write_analysis_manifest
from ..loader import load_jsonl
from .artifact_validity import evaluate_artifact_validity
from .agreement import FieldMetric, analyze_agreement
from .anchors import (
    analyze_anchors,
    anchor_summary,
    load_review_decisions,
)
from .common import (
    assignment_coverage_by_scenario,
    annotation_files,
    flatten_annotations,
    load_annotation_documents,
    load_hidden_by_scenario,
    load_scenarios,
)
from .confusion import confusion_matrices
from .critical_violations import CriticalViolation, find_critical_violations, violation_rates
from .disagreement import DisagreementItem, build_disagreement_queue
from .orthogonality import OrthogonalityResult, analyze_orthogonality, orthogonality_summary
from .report import build_construct_report, build_disagreement_report


def _write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for row in rows:
            stream.write(json.dumps(dict(row), ensure_ascii=False, sort_keys=True) + "\n")


def write_metrics(path: Path, metrics: list[FieldMetric]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(FieldMetric.__dataclass_fields__)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        for metric in metrics:
            row = metric.to_dict()
            row["rater_count_distribution"] = json.dumps(
                row["rater_count_distribution"], sort_keys=True
            )
            writer.writerow(row)


def run_analysis(
    *,
    annotations_dir: str | Path,
    scenarios_path: str | Path | Iterable[str | Path],
    coverage_path: str | Path,
    pairs_path: str | Path,
    output_dir: str | Path,
    only: set[str] | None = None,
    quality_thresholds_path: str | Path | None = None,
    repository_root: str | Path | None = None,
    assignments_root: str | Path | None = None,
    anchor_reference_path: str | Path | None = None,
    anchor_review_decisions_path: str | Path | None = None,
    manual_path: str | Path | None = None,
) -> dict[str, Any]:
    only = only or {"agreement", "violations", "orthogonality", "anchors", "disagreement", "report"}
    scenario_paths = (
        [Path(scenarios_path)]
        if isinstance(scenarios_path, (str, Path))
        else [Path(path) for path in scenarios_path]
    )
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    quality_thresholds_path = Path(
        quality_thresholds_path
        or Path(__file__).parents[1] / "settings" / "quality_thresholds_v1.json"
    )
    anchor_reference_path = Path(
        anchor_reference_path or Path(coverage_path).parent / "anchor_reference.jsonl"
    )
    anchor_review_decisions_path = Path(
        anchor_review_decisions_path
        or Path(__file__).parents[1] / "adjudication" / "anchor_review_decisions.jsonl"
    )
    artifact_validity = evaluate_artifact_validity(
        scenarios_paths=scenario_paths,
        annotations_dir=annotations_dir,
        coverage_path=coverage_path,
        pairs_path=pairs_path,
        assignments_root=assignments_root,
        anchor_reference_path=anchor_reference_path,
        anchor_review_decisions_path=anchor_review_decisions_path,
        manual_path=manual_path,
    )
    (root / "artifact_validity.json").write_text(
        json.dumps(artifact_validity, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    required_inputs = (
        "scenario_validator",
        "annotation_validator",
        "pair_design_validator",
        "coverage_validator",
        "anchor_reference_validator",
        "anchor_review_decision_validator",
        "manual_provenance",
    )
    failed_inputs = [
        name for name in required_inputs
        if artifact_validity["checks"][name] != "PASS"
    ]
    if failed_inputs:
        raise ValueError(
            "artifact validation failed before analysis: "
            + ", ".join(failed_inputs)
            + f"; see {root / 'artifact_validity.json'}"
        )
    scenarios = {
        scenario_id: scenario
        for path in scenario_paths
        for scenario_id, scenario in load_scenarios(path).items()
    }
    documents = load_annotation_documents(
        annotations_dir,
        validate=True,
        scenarios=scenarios,
        require_scenario_context=True,
    )
    rows = flatten_annotations(documents)
    if not rows:
        raise ValueError("annotation documents contain no records")
    coverage = load_hidden_by_scenario(coverage_path)
    pairs = load_jsonl(pairs_path)
    anchors = load_jsonl(anchor_reference_path)
    review_decisions = load_review_decisions(anchor_review_decisions_path)
    assignment_coverage = (
        assignment_coverage_by_scenario(assignments_root)
        if assignments_root is not None
        else None
    )
    expected_anchor_annotators = (
        {
            scenario_id: modules.get("A", set())
            for scenario_id, modules in assignment_coverage.items()
        }
        if assignment_coverage is not None
        else None
    )

    metrics = analyze_agreement(rows)
    violations = find_critical_violations(rows, scenarios, coverage)
    orthogonality = analyze_orthogonality(
        rows, pairs, scenarios=scenarios, assignment_coverage=assignment_coverage
    )
    anchor_audit = analyze_anchors(
        anchors,
        rows,
        expected_annotators=expected_anchor_annotators,
        review_decisions=review_decisions,
    )
    anchors_summary = anchor_summary(anchor_audit)
    disagreements = build_disagreement_queue(rows)

    if "agreement" in only:
        write_metrics(root / "field_metrics.csv", metrics)
        (root / "confusion_matrices.json").write_text(
            json.dumps(confusion_matrices(rows), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    if "violations" in only:
        _write_jsonl(root / "critical_violations.jsonl", (item.to_dict() for item in violations))
        (root / "semantic_violation_rates.json").write_text(
            json.dumps(
                violation_rates(violations, rows, scenarios, coverage),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ) + "\n",
            encoding="utf-8",
        )
    if "orthogonality" in only:
        _write_jsonl(root / "orthogonality.jsonl", (item.to_dict() for item in orthogonality))
        (root / "orthogonality_summary.json").write_text(
            json.dumps(orthogonality_summary(orthogonality), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    if "anchors" in only:
        _write_jsonl(root / "anchor_audit.jsonl", anchor_audit)
        (root / "anchor_summary.json").write_text(
            json.dumps(anchors_summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
    if "disagreement" in only:
        _write_jsonl(root / "disagreement_queue.jsonl", (item.to_dict() for item in disagreements))
        (root / "disagreement_report.md").write_text(
            build_disagreement_report(disagreements), encoding="utf-8", newline="\n"
        )
    if "report" in only:
        (root / "scenario_annotation_report.md").write_text(
            build_construct_report(metrics, violations, orthogonality, disagreements),
            encoding="utf-8",
            newline="\n",
        )
    manual_versions = {str(document["manual_version"]) for document in documents}
    if len(manual_versions) != 1:
        raise ValueError(f"analysis inputs contain multiple manual versions: {sorted(manual_versions)}")
    manual_version = manual_versions.pop()
    resolved_manual_path = Path(
        manual_path
        or Path(__file__).parents[1] / "manuals" / f"coding_manual_v{manual_version}.md"
    )
    manifest = build_analysis_manifest(
        annotation_paths=annotation_files(annotations_dir),
        scenario_paths=scenario_paths,
        coverage_path=coverage_path,
        pair_design_path=pairs_path,
        quality_thresholds_path=quality_thresholds_path,
        manual_version=manual_version,
        repository_root=repository_root or Path(__file__).parents[3],
        anchor_reference_path=anchor_reference_path,
        anchor_review_decisions_path=anchor_review_decisions_path,
        manual_path=resolved_manual_path,
        assignments_root=assignments_root,
    )
    write_analysis_manifest(root / "analysis_manifest.json", manifest)
    return {
        "documents": len(documents),
        "records": len(rows),
        "fields": len(metrics),
        "violations": len(violations),
        "orthogonality_checks": len(orthogonality),
        "anchor_checks": anchors_summary["expected_checks"],
        "anchor_unexplained_mismatches": anchors_summary["unexplained_mismatches"],
        "disagreements": len(disagreements),
        "artifact_validity": artifact_validity["status"],
    }
