from pathlib import Path

from research.scenario_annotation.freeze import FreezeBlockedError, freeze_representation
from research.scenario_annotation.gates import evaluate_manual_ready, evaluate_semantic_reliability


def test_manual_ready_gate_fails_without_independent_annotations_or_human_revision(tmp_path: Path) -> None:
    package = Path(__file__).parents[1]
    result = evaluate_manual_ready(
        manual_path=package / "manuals" / "coding_manual_v0.1.md",
        scenarios_path=package / "scenarios" / "calibration.jsonl",
        coverage_path=package / "hidden" / "coverage_tags.jsonl",
        annotations_dir=package / "annotations" / "calibration",
        analysis_dir=tmp_path / "analysis",
        revision_log_path=tmp_path / "representation_revision_log.jsonl",
    )
    assert result.status == "FAIL"
    failed = {check.name for check in result.checks if not check.passed}
    assert "coding_manual_v1_candidate" in failed
    assert "independent_primary_annotators" in failed
    assert "calibration_analysis_outputs" in failed
    assert "resolved_revision_decisions" in failed


def test_current_calibration_corpus_passes_gate_schema_and_coverage_checks(tmp_path: Path) -> None:
    package = Path(__file__).parents[1]
    result = evaluate_manual_ready(
        manual_path=package / "manuals" / "coding_manual_v0.1.md",
        scenarios_path=package / "scenarios" / "calibration.jsonl",
        coverage_path=package / "hidden" / "coverage_tags.jsonl",
        annotations_dir=package / "annotations" / "calibration",
        analysis_dir=tmp_path / "analysis",
        revision_log_path=tmp_path / "representation_revision_log.jsonl",
    )
    checks = {check.name: check for check in result.checks}
    assert checks["calibration_scenario_schema_and_count"].passed
    assert checks["critical_boundary_coverage"].passed


def test_semantic_gate_fails_before_formal_corpus_and_blind_validation(tmp_path: Path) -> None:
    result = evaluate_semantic_reliability(
        main_scenarios_path=tmp_path / "main.jsonl",
        edge_scenarios_path=tmp_path / "edge.jsonl",
        annotations_dir=tmp_path / "annotations",
        analysis_dir=tmp_path / "analysis",
    )
    assert result.status == "FAIL"
    assert all(not check.passed for check in result.checks)


def test_freeze_refuses_to_create_manifest_before_gates_pass(tmp_path: Path) -> None:
    output = tmp_path / "representation_semantics_v1.0.json"
    try:
        freeze_representation(
            repository_root=Path(__file__).parents[3],
            gate_a_path=tmp_path / "gate_a.json",
            gate_b_path=tmp_path / "gate_b.json",
            manual_path=tmp_path / "manual.md",
            main_scenarios_path=tmp_path / "main.jsonl",
            edge_scenarios_path=tmp_path / "edge.jsonl",
            gold_path=tmp_path / "gold.jsonl",
            revision_log_path=tmp_path / "revisions.jsonl",
            field_metrics_path=tmp_path / "field_metrics.csv",
            critical_violation_report_path=tmp_path / "violations.jsonl",
            output_path=output,
        )
    except FreezeBlockedError:
        pass
    else:
        raise AssertionError("freeze should have been blocked")
    assert not output.exists()
