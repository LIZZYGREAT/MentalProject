import json
from pathlib import Path

from research.scenario_annotation.analysis.common import AnnotationRow
from research.scenario_annotation.analysis.critical_violations import find_critical_violations
from research.scenario_annotation.analysis.orthogonality import analyze_orthogonality
from research.scenario_annotation.analysis.pipeline import run_analysis
from research.scenario_annotation.corpus import write_calibration
from research.scenario_annotation.loader import load_jsonl


def _row(scenario: str, annotator: str, variable: str, label, target: str, module: str, **extra) -> AnnotationRow:
    record = {
        "annotation_id": f"{scenario}:{module}:{variable}:{annotator}",
        "manual_version": "0.1",
        "evidence_refs": [],
        **extra,
    }
    return AnnotationRow(scenario, module, annotator, target, variable, label, record["annotation_id"], record)


def test_critical_boundary_detectors_use_hidden_design_only_in_analysis(tmp_path) -> None:
    root = tmp_path / "corpus"
    write_calibration(root)
    scenarios = {row["scenario_id"]: row for row in load_jsonl(root / "scenarios" / "calibration.jsonl")}
    coverage = {row["scenario_id"]: set(row["coverage_tags"]) for row in load_jsonl(root / "hidden" / "coverage_tags.jsonl")}
    rows = [
        _row("CAL_015", "AI-A", "RECOVERY_OCCURRENCE", "ACTIVE", "E_EMPTY_WINDOW", "A"),
        _row("CAL_018", "AI-A", "C_EXEC", "LOW", "E_HARD_TASK", "B", evidence_span="hard task"),
        _row("CAL_022", "AI-A", "SUPPORT_GATE", "SUPPORTIVE", "BOT_022", "C"),
    ]
    kinds = {item.violation_type for item in find_critical_violations(rows, scenarios, coverage)}
    assert kinds == {
        "FreeTimeAsRecoveryError",
        "UnsupportedAppraisalInference",
        "TaskHelpAsSupportError",
    }


def test_orthogonality_flags_expected_change_and_invariant_spillover() -> None:
    rows = [
        _row("A", "AI-A", "C_EXEC", "NO_EVIDENCE", "T", "B"),
        _row("B", "AI-A", "C_EXEC", "LOW", "T", "B"),
        _row("A", "AI-A", "D_POT", "HIGH", "T", "A"),
        _row("B", "AI-A", "D_POT", "LOW", "T", "A"),
    ]
    pair = {
        "pair_id": "P",
        "scenario_ids": ["A", "B"],
        "expected_sensitive_constructs": ["C_EXEC"],
        "expected_invariant_constructs": ["D_POT"],
    }
    statuses = {item.status for item in analyze_orthogonality(rows, [pair])}
    assert statuses == {"EXPECTED_CHANGE", "POTENTIAL_SPILLOVER"}


def _annotation_document(annotator: str, label: str, evidence_ref: str) -> dict:
    record = {
        "annotation_id": f"CAL_019:B:C_EXEC:{annotator}",
        "scenario_id": "CAL_019",
        "scenario_version": "0.1",
        "annotation_module": "B",
        "target_ref": "E_HARD_TASK_LOW_CEXEC",
        "variable": "C_EXEC",
        "label": label,
        "evidence_refs": [evidence_ref],
        "evidence_span": "我完全不知道怎么下手，今天肯定做不出来",
        "evidence_strength": "STRONG",
        "scope": "EPISODE",
        "ambiguity_flag": False,
        "annotator_confidence": "HIGH",
        "manual_version": "0.1",
        "annotator_id": annotator,
        "annotation_round": "CALIBRATION",
        "created_at": "2026-09-22T12:00:00+08:00",
    }
    return {
        "schema_version": "1.0",
        "scenario_id": "CAL_019",
        "annotation_module": "B",
        "annotator_id": annotator,
        "annotation_round": "CALIBRATION",
        "manual_version": "0.1",
        "scenario_validity": {
            "scenario_valid": "YES",
            "scenario_plausibility": "HIGH",
            "contradiction_present": "NO",
        },
        "records": [record],
    }


def test_full_analysis_pipeline_writes_all_required_outputs(tmp_path) -> None:
    corpus_root = tmp_path / "corpus"
    write_calibration(corpus_root)
    annotations = tmp_path / "annotations"
    annotations.mkdir()
    documents = [
        _annotation_document("AI-A", "LOW", "MSG_019"),
        _annotation_document("AI-B", "LOW", "MSG_019"),
        _annotation_document("AI-C", "MEDIUM", "MSG_019"),
        _annotation_document("Human", "LOW", "MSG_019"),
    ]
    for index, document in enumerate(documents):
        (annotations / f"annotation_{index}.json").write_text(
            json.dumps(document, ensure_ascii=False), encoding="utf-8"
        )
    output = tmp_path / "output"
    summary = run_analysis(
        annotations_dir=annotations,
        scenarios_path=corpus_root / "scenarios" / "calibration.jsonl",
        coverage_path=corpus_root / "hidden" / "coverage_tags.jsonl",
        pairs_path=corpus_root / "hidden" / "pair_design.jsonl",
        output_dir=output,
    )
    assert summary["documents"] == 4
    assert summary["records"] == 4
    expected = {
        "field_metrics.csv",
        "confusion_matrices.json",
        "critical_violations.jsonl",
        "critical_violation_rates.json",
        "orthogonality.jsonl",
        "disagreement_queue.jsonl",
        "disagreement_report.md",
        "scenario_annotation_report.md",
    }
    assert expected == {path.name for path in output.iterdir()}
    assert "Construct: C_EXEC" in (output / "scenario_annotation_report.md").read_text(encoding="utf-8")
