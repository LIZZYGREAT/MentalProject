import csv
import json
from pathlib import Path

import pytest

from research.scenario_annotation.analysis.agreement import (
    _ordinal_distance,
    analyze_agreement,
    krippendorff_alpha,
)
from research.scenario_annotation.analysis.common import AnnotationRow
from research.scenario_annotation.analysis.disagreement import build_disagreement_queue
from research.scenario_annotation.analysis.pipeline import write_metrics
from research.scenario_annotation.analysis.report import build_construct_report


FIXTURES = Path(__file__).parent / "fixtures"


def row(scenario: str, annotator: str, variable: str, label, module: str = "B") -> AnnotationRow:
    record = {
        "annotation_id": f"{scenario}:{annotator}:{variable}",
        "manual_version": "0.1",
        "evidence_refs": [],
    }
    return AnnotationRow(scenario, module, annotator, "TARGET", variable, label, record["annotation_id"], record)


def test_krippendorff_nominal_perfect_agreement() -> None:
    alpha = krippendorff_alpha(
        [["A", "A", "A"], ["B", "B", "B"]],
        lambda left, right: 0.0 if left == right else 1.0,
    )
    assert alpha == 1.0


def test_krippendorff_published_nominal_example() -> None:
    fixture = json.loads((FIXTURES / "krippendorff_2013_nominal_alpha.json").read_text())
    alpha = krippendorff_alpha(
        fixture["ratings_by_unit"],
        lambda left, right: 0.0 if left == right else 1.0,
    )
    assert alpha == pytest.approx(fixture["expected_alpha"], abs=0.00005)


def test_krippendorff_published_ordinal_example() -> None:
    fixture = json.loads((FIXTURES / "hayes_krippendorff_2007_ordinal_alpha.json").read_text())
    alpha_type, distance = _ordinal_distance((0, 1, 2, 3), fixture["ratings_by_unit"])
    alpha = krippendorff_alpha(fixture["ratings_by_unit"], distance)
    assert alpha_type == "ordinal"
    assert alpha == pytest.approx(fixture["expected_alpha"], abs=0.0001)


def test_krippendorff_normalizes_units_with_unequal_rater_counts() -> None:
    alpha = krippendorff_alpha(
        [["A", "A"], ["A", "B", "B", "B"]],
        lambda left, right: 0.0 if left == right else 1.0,
    )
    assert alpha == pytest.approx(4 / 9)


def test_krippendorff_ignores_missing_and_single_rater_units() -> None:
    distance = lambda left, right: 0.0 if left == right else 1.0
    alpha = krippendorff_alpha(
        [["A", "A", None], ["A", None], ["B", "B"]], distance
    )
    assert alpha == 1.0
    assert krippendorff_alpha([["A"], [None, "B"]], distance) is None


def test_krippendorff_systematic_disagreement_is_negative() -> None:
    alpha = krippendorff_alpha(
        [["A", "B"], ["B", "A"]],
        lambda left, right: 0.0 if left == right else 1.0,
    )
    assert alpha == -0.5


def test_field_metrics_include_ordinal_major_disagreement() -> None:
    rows = [
        row("S1", "AI-A", "C_EXEC", "LOW"),
        row("S1", "AI-B", "C_EXEC", "HIGH"),
        row("S2", "AI-A", "C_EXEC", "MEDIUM"),
        row("S2", "AI-B", "C_EXEC", "MEDIUM"),
    ]
    metric = analyze_agreement(rows)[0]
    assert metric.alpha_type == "ordinal"
    assert metric.raw_agreement == 0.5
    assert metric.major_disagreement_rate == 0.5


def test_field_metrics_expose_rater_count_coverage_and_csv_round_trips_it(tmp_path) -> None:
    rows = [
        row("S1", "AI-A", "C_EXEC", "LOW"),
        row("S1", "AI-B", "C_EXEC", "LOW"),
        row("S2", "AI-A", "C_EXEC", "MEDIUM"),
        row("S3", "AI-A", "C_EXEC", "HIGH"),
        row("S3", "AI-B", "C_EXEC", "HIGH"),
        row("S3", "AI-C", "C_EXEC", "HIGH"),
        row("S3", "Human", "C_EXEC", "HIGH"),
    ]
    metric = analyze_agreement(rows)[0]
    assert metric.n_units == 3
    assert metric.pairable_units == 2
    assert metric.min_raters_per_unit == 1
    assert metric.max_raters_per_unit == 4
    assert metric.mean_raters_per_unit == pytest.approx(7 / 3)
    assert metric.rater_count_distribution == {1: 1, 2: 1, 4: 1}
    report = build_construct_report([metric], [], [], [])
    assert "Units / Pairable Units: 3 / 2" in report
    assert "Raters per Unit: min=1; mean=2.333; max=4" in report
    assert 'Rater Count Distribution: `{"1": 1, "2": 1, "4": 1}`' in report

    output = tmp_path / "field_metrics.csv"
    write_metrics(output, [metric])
    with output.open(encoding="utf-8", newline="") as stream:
        encoded = next(csv.DictReader(stream))["rater_count_distribution"]
    assert json.loads(encoded) == {"1": 1, "2": 1, "4": 1}


def test_disagreement_queue_never_selects_a_majority_gold() -> None:
    rows = [
        row("S1", "AI-A", "C_EXEC", "LOW"),
        row("S1", "AI-B", "C_EXEC", "LOW"),
        row("S1", "Human", "C_EXEC", "HIGH"),
    ]
    item = build_disagreement_queue(rows)[0]
    assert item.severity == "MAJOR"
    assert item.status == "PENDING_ADJUDICATION"
    assert "gold" not in item.to_dict()
