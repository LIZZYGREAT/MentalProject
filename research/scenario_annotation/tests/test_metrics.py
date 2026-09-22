from research.scenario_annotation.analysis.agreement import analyze_agreement, krippendorff_alpha
from research.scenario_annotation.analysis.common import AnnotationRow
from research.scenario_annotation.analysis.disagreement import build_disagreement_queue


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
