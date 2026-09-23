import json
from pathlib import Path

import pytest

from research.scenario_annotation.analysis.common import AnnotationRow
from research.scenario_annotation.analysis.critical_violations import find_critical_violations, violation_rates
from research.scenario_annotation.analysis.orthogonality import analyze_orthogonality, orthogonality_summary
from research.scenario_annotation.analysis.pipeline import run_analysis
from research.scenario_annotation.analysis_manifest import verify_analysis_manifest_current
from research.scenario_annotation.corpus import write_calibration
from research.scenario_annotation.loader import load_jsonl


PACKAGE_ROOT = Path(__file__).parents[1]


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
    rates = violation_rates(find_critical_violations(rows, scenarios, coverage), rows, scenarios, coverage)
    assert set(rates) == {
        "UnsupportedAppraisalInferenceRate",
        "ParentChildObligationDoubleCountRate",
        "FreeTimeAsRecoveryErrorRate",
        "MissedCourseAutomaticObligationErrorRate",
        "TaskHelpAsSupportErrorRate",
    }
    assert rates["TaskHelpAsSupportErrorRate"] == {
        "count": 1,
        "eligible_count": 1,
        "rate": 1.0,
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
        "comparison_mode": "WITHIN_ANNOTATOR",
        "pair_kind": "MINIMAL_CONTRAST",
        "expected_sensitive_constructs": ["C_EXEC"],
        "expected_invariant_constructs": ["D_POT"],
    }
    results = analyze_orthogonality(rows, [pair])
    assert {item.status for item in results} == {"EVALUATED"}
    assert {item.outcome for item in results} == {"EXPECTED_CHANGE", "POTENTIAL_SPILLOVER"}


def test_presentation_equivalence_is_evaluated_between_counterbalanced_groups() -> None:
    rows = [
        _row("LEFT", "AI-A", "LIFECYCLE", "SKIPPED", "COURSE", "A"),
        _row("LEFT", "AI-C", "LIFECYCLE", "SKIPPED", "COURSE", "A"),
        _row("RIGHT", "AI-B", "LIFECYCLE", "SKIPPED", "COURSE", "A"),
        _row("RIGHT", "Human", "LIFECYCLE", "SKIPPED", "COURSE", "A"),
    ]
    pair = {
        "pair_id": "PRESENTATION",
        "scenario_ids": ["LEFT", "RIGHT"],
        "comparison_mode": "BETWEEN_GROUPS",
        "pair_kind": "PRESENTATION_EQUIVALENCE",
        "expected_sensitive_constructs": [],
        "expected_invariant_constructs": ["LIFECYCLE"],
        "target_mapping": {
            "LIFECYCLE": {"left_target_ref": "COURSE", "right_target_ref": "COURSE"}
        },
    }
    result = analyze_orthogonality(rows, [pair])[0]
    assert result.comparison_mode == "BETWEEN_GROUPS"
    assert result.status == "EVALUATED"
    assert result.outcome == "INVARIANT_HELD"
    assert len(result.left_labels) == len(result.right_labels) == 2


def test_orthogonality_uses_record_attribute_and_explicit_target_refs() -> None:
    rows = [
        _row("LEFT", "AI-A", "EXECUTION_EXPOSURE", "PARTIAL", "TARGET_A", "A", partial_encoding_basis="ACTUAL_INTERVAL"),
        _row("RIGHT", "AI-A", "EXECUTION_EXPOSURE", "PARTIAL", "TARGET_B", "A", partial_encoding_basis="FRACTION_ONLY"),
        _row("LEFT", "AI-A", "EXECUTION_EXPOSURE", "ACTIVE", "OTHER_A", "A", partial_encoding_basis="OTHER_EXPLICIT"),
        _row("RIGHT", "AI-A", "EXECUTION_EXPOSURE", "ACTIVE", "OTHER_B", "A", partial_encoding_basis="OTHER_EXPLICIT"),
    ]
    pair = {
        "pair_id": "ATTRIBUTE",
        "scenario_ids": ["LEFT", "RIGHT"],
        "comparison_mode": "WITHIN_ANNOTATOR",
        "pair_kind": "MINIMAL_CONTRAST",
        "expected_sensitive_constructs": [],
        "expected_invariant_constructs": ["PARTIAL_ENCODING_BASIS"],
        "target_mapping": {
            "PARTIAL_ENCODING_BASIS": {
                "target_variable": "EXECUTION_EXPOSURE",
                "record_attribute": "partial_encoding_basis",
                "left_target_ref": "TARGET_A",
                "right_target_ref": "TARGET_B",
            }
        },
    }
    result = analyze_orthogonality(rows, [pair])[0]
    assert result.status == "EVALUATED"
    assert result.left_labels[0]["label"] == "ACTUAL_INTERVAL"
    assert result.right_labels[0]["label"] == "FRACTION_ONLY"
    assert result.outcome == "POTENTIAL_SPILLOVER"


def test_orthogonality_reports_each_expected_check_when_annotations_are_missing() -> None:
    pair = {
        "pair_id": "MISSING",
        "scenario_ids": ["LEFT", "RIGHT"],
        "comparison_mode": "WITHIN_ANNOTATOR",
        "pair_kind": "MINIMAL_CONTRAST",
        "expected_sensitive_constructs": ["LIFECYCLE"],
        "expected_invariant_constructs": [],
    }
    results = analyze_orthogonality([], [pair])
    summary = orthogonality_summary(results)
    assert len(results) == 1
    assert results[0].status == "NOT_COMPARABLE"
    assert summary["expected_checks"] == 1
    assert summary["evaluated_checks"] == 0
    assert summary["missing_checks"] == 1


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
        "semantic_violation_rates.json",
        "artifact_validity.json",
        "orthogonality.jsonl",
        "orthogonality_summary.json",
        "disagreement_queue.jsonl",
        "disagreement_report.md",
        "scenario_annotation_report.md",
        "analysis_manifest.json",
    }
    assert expected == {path.name for path in output.iterdir()}
    generated_orthogonality = json.loads(
        (output / "orthogonality_summary.json").read_text(encoding="utf-8")
    )
    assert generated_orthogonality["missing_checks"] > 0
    assert "Construct: C_EXEC" in (output / "scenario_annotation_report.md").read_text(encoding="utf-8")

    annotation_path = annotations / "annotation_0.json"
    annotation_path.write_text(annotation_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="stale analysis manifest"):
        verify_analysis_manifest_current(
            output / "analysis_manifest.json",
            annotations_dir=annotations,
            scenario_paths=[corpus_root / "scenarios" / "calibration.jsonl"],
            coverage_path=corpus_root / "hidden" / "coverage_tags.jsonl",
            pair_design_path=corpus_root / "hidden" / "pair_design.jsonl",
            quality_thresholds_path=PACKAGE_ROOT / "settings" / "quality_thresholds_v1.json",
        )
