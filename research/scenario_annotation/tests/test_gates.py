from pathlib import Path
import json

import pytest

from research.scenario_annotation.annotation_catalog import field_specs
from research.scenario_annotation.freeze import FreezeBlockedError, freeze_representation
from research.scenario_annotation.gates import (
    evaluate_manual_ready,
    evaluate_representation_freeze,
    evaluate_semantic_reliability,
    evaluate_violation_thresholds,
)


def _write_submission_fixture(root: Path, *, round_name: str, manual_version: str) -> list[dict]:
    documents = []
    scenario = {
        "scenario_id": "S1",
        "scenario_version": "1.0",
        "annotation_modules": ["C"],
        "bot_response_units": [{"response_unit_ref": "BOT"}],
    }
    for annotator in ("AI-A", "AI-B", "AI-C", "Human"):
        assignment = root / annotator.lower().replace("-", "_")
        assignment.mkdir(parents=True)
        (assignment / "module_c.jsonl").write_text(json.dumps(scenario) + "\n", encoding="utf-8")
        (assignment / "manifest.json").write_text(
            json.dumps(
                {
                    "annotator_id": annotator,
                    "annotation_round": round_name,
                    "files": [{"module": "C", "path": "module_c.jsonl"}],
                }
            ),
            encoding="utf-8",
        )
        records = [
            {
                "annotation_id": f"S1:C:BOT:{spec.variable}:{annotator}",
                "target_ref": "BOT",
                "variable": spec.variable,
                "label": spec.options[0],
            }
            for spec in field_specs("C")
        ]
        documents.append(
            {
                "scenario_id": "S1",
                "annotation_module": "C",
                "annotator_id": annotator,
                "annotation_round": round_name,
                "manual_version": manual_version,
                "records": records,
            }
        )
    return documents


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


def test_gate_b_uses_versioned_semantic_thresholds() -> None:
    settings = {
        "semantic_misunderstanding_max_rates": {"TaskHelpAsSupportErrorRate": 0.05},
    }
    acceptable = {
        "TaskHelpAsSupportErrorRate": {"rate": 0.04},
    }
    passed, exceeded = evaluate_violation_thresholds(acceptable, settings)
    assert passed
    assert exceeded == {}

    rejected = {
        "TaskHelpAsSupportErrorRate": {"rate": 0.06},
    }
    passed, exceeded = evaluate_violation_thresholds(rejected, settings)
    assert not passed
    assert exceeded["TaskHelpAsSupportErrorRate"]["category"] == "SEMANTIC_MISUNDERSTANDING"


@pytest.mark.parametrize("damage", ["missing_scenario", "missing_variable"])
def test_gate_a_rejects_incomplete_assignment_sets(tmp_path: Path, monkeypatch, damage: str) -> None:
    package = Path(__file__).parents[1]
    assignments = tmp_path / "assignments"
    documents = _write_submission_fixture(assignments, round_name="CALIBRATION", manual_version="0.1")
    if damage == "missing_scenario":
        documents.pop()
    else:
        documents[0]["records"].pop()
    monkeypatch.setattr("research.scenario_annotation.gates.load_annotation_documents", lambda *args, **kwargs: documents)
    result = evaluate_manual_ready(
        manual_path=package / "manuals" / "coding_manual_v0.1.md",
        scenarios_path=package / "scenarios" / "calibration.jsonl",
        coverage_path=package / "hidden" / "coverage_tags.jsonl",
        annotations_dir=tmp_path / "annotations",
        analysis_dir=tmp_path / "analysis",
        revision_log_path=tmp_path / "revisions.jsonl",
        assignments_root=assignments,
    )
    check = next(item for item in result.checks if item.name == "independent_primary_annotators")
    assert not check.passed
    assert "missing=" in check.detail or "incomplete=" in check.detail


def test_gate_b_rejects_incomplete_formal_assignment_set(tmp_path: Path, monkeypatch) -> None:
    assignments = tmp_path / "assignments"
    documents = _write_submission_fixture(assignments, round_name="VALIDATION", manual_version="1.0")
    documents.pop()
    monkeypatch.setattr("research.scenario_annotation.gates.load_annotation_documents", lambda *args, **kwargs: documents)
    main = tmp_path / "main.jsonl"
    edge = tmp_path / "edge.jsonl"
    main.write_text("", encoding="utf-8")
    edge.write_text("", encoding="utf-8")
    result = evaluate_semantic_reliability(
        main_scenarios_path=main,
        edge_scenarios_path=edge,
        annotations_dir=tmp_path / "annotations",
        analysis_dir=tmp_path / "analysis",
        assignments_root=assignments,
    )
    check = next(item for item in result.checks if item.name == "blind_independent_annotations")
    assert not check.passed
    assert "missing=" in check.detail


def test_freeze_gold_check_rejects_one_missing_target_variable(tmp_path: Path) -> None:
    scenarios = []
    reference = []
    for index in range(96):
        scenario_id = f"S{index:03d}"
        scenarios.append(
            {
                "scenario_id": scenario_id,
                "scenario_version": "1.0",
                "annotation_modules": ["C"],
                "bot_response_units": [{"response_unit_ref": f"BOT{index:03d}"}],
            }
        )
        for spec in field_specs("C"):
            reference.append(
                {
                    "reference_id": f"R:{scenario_id}:{spec.variable}",
                    "scenario_id": scenario_id,
                    "target_ref": f"BOT{index:03d}",
                    "variable": spec.variable,
                    "gold_label": spec.options[0],
                    "resolution_mode": "UNANIMOUS",
                    "source_annotation_ids": ["A", "B"],
                    "manual_version": "1.0",
                    "scenario_version": "1.0",
                }
            )
    reference.pop()
    main = tmp_path / "main.jsonl"
    edge = tmp_path / "edge.jsonl"
    main.write_text("".join(json.dumps(row) + "\n" for row in scenarios[:72]), encoding="utf-8")
    edge.write_text("".join(json.dumps(row) + "\n" for row in scenarios[72:]), encoding="utf-8")
    gold = tmp_path / "gold.jsonl"
    gold.write_text("".join(json.dumps(row) + "\n" for row in reference), encoding="utf-8")
    result = evaluate_representation_freeze(
        gate_a_path=tmp_path / "gate_a.json",
        gate_b_path=tmp_path / "gate_b.json",
        manual_path=tmp_path / "manual.md",
        main_scenarios_path=main,
        edge_scenarios_path=edge,
        gold_path=gold,
        revision_log_path=tmp_path / "revisions.jsonl",
    )
    check = next(item for item in result.checks if item.name == "adjudicated_reference_set")
    assert not check.passed
    assert "missing_reference_keys" in check.detail
