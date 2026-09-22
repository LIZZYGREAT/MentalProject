from pathlib import Path

from research.scenario_annotation.gates import evaluate_manual_ready


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
