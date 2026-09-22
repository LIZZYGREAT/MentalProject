"""Validated Stage 1 analysis pipeline and artifact writers."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from ..loader import load_jsonl
from .agreement import FieldMetric, analyze_agreement
from .common import (
    flatten_annotations,
    load_annotation_documents,
    load_hidden_by_scenario,
    load_scenarios,
)
from .confusion import confusion_matrices
from .critical_violations import CriticalViolation, find_critical_violations, violation_rates
from .disagreement import DisagreementItem, build_disagreement_queue
from .orthogonality import OrthogonalityResult, analyze_orthogonality
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
        writer.writerows(metric.to_dict() for metric in metrics)


def run_analysis(
    *,
    annotations_dir: str | Path,
    scenarios_path: str | Path,
    coverage_path: str | Path,
    pairs_path: str | Path,
    output_dir: str | Path,
    only: set[str] | None = None,
) -> dict[str, Any]:
    only = only or {"agreement", "violations", "orthogonality", "disagreement", "report"}
    scenarios = load_scenarios(scenarios_path)
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
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)

    metrics = analyze_agreement(rows)
    violations = find_critical_violations(rows, scenarios, coverage)
    orthogonality = analyze_orthogonality(rows, pairs)
    disagreements = build_disagreement_queue(rows)

    if "agreement" in only:
        write_metrics(root / "field_metrics.csv", metrics)
        (root / "confusion_matrices.json").write_text(
            json.dumps(confusion_matrices(rows), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    if "violations" in only:
        _write_jsonl(root / "critical_violations.jsonl", (item.to_dict() for item in violations))
        (root / "critical_violation_rates.json").write_text(
            json.dumps(violation_rates(violations, rows), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    if "orthogonality" in only:
        _write_jsonl(root / "orthogonality.jsonl", (item.to_dict() for item in orthogonality))
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
    return {
        "documents": len(documents),
        "records": len(rows),
        "fields": len(metrics),
        "violations": len(violations),
        "orthogonality_checks": len(orthogonality),
        "disagreements": len(disagreements),
    }
