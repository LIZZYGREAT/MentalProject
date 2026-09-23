from pathlib import Path
import json

import pytest

from research.scenario_annotation.annotation_catalog import field_specs
from research.scenario_annotation.artifact_fingerprint import sha256_file, sha256_fileset
from research.scenario_annotation.freeze import FreezeBlockedError, freeze_representation
from research.scenario_annotation.analysis.critical_violations import violation_rates
from research.scenario_annotation.gates import (
    MANUAL_GATE_CONSTRUCTS,
    _check_field_metrics,
    evaluate_manual_ready,
    evaluate_representation_freeze,
    evaluate_semantic_reliability,
    evaluate_violation_thresholds,
)
from research.scenario_annotation.validation import Validator


def _write_submission_fixture(root: Path, *, round_name: str, manual_version: str) -> list[dict]:
    documents = []
    manual_path = root.parent.parent / "manuals" / f"coding_manual_v{manual_version}.md"
    manual_path.parent.mkdir(parents=True, exist_ok=True)
    manual_path.write_text("test coding manual\n", encoding="utf-8")
    manual_sha = sha256_file(manual_path)
    scenario = {
        "scenario_id": "S1",
        "scenario_version": "1.0",
        "annotation_modules": ["C"],
        "bot_response_units": [{"response_unit_ref": "BOT"}],
    }
    for annotator in ("AI-A", "AI-B", "AI-C", "Human"):
        assignment = root / annotator.lower().replace("-", "_")
        assignment.mkdir(parents=True)
        manifest_files = []
        for module in ("A", "B", "C"):
            path = assignment / f"module_{module.lower()}.jsonl"
            rows = [scenario] if module == "C" else []
            path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            manifest_files.append(
                {
                    "module": module,
                    "path": path.name,
                    "scenario_count": len(rows),
                    "sha256": sha256_file(path),
                }
            )
        (assignment / "manifest.json").write_text(
            json.dumps(
                {
                    "assignment_version": "1.0",
                    "annotator_id": annotator,
                    "annotation_round": round_name,
                    "manual_version": manual_version,
                    "manual_sha256": manual_sha,
                    "scenario_version": "1.0",
                    "randomization_seed": 17,
                    "counterbalance_rule": "fixture",
                    "files": manifest_files,
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
                "manual_sha256": manual_sha,
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


def test_freeze_requires_reference_set_manifest_to_bind_gate_b_analysis(tmp_path: Path) -> None:
    main = tmp_path / "main.jsonl"
    edge = tmp_path / "edge.jsonl"
    scenarios = [
        {
            "scenario_id": f"S{index:03d}",
            "annotation_modules": ["C"],
            "bot_response_units": [{"response_unit_ref": f"BOT{index:03d}"}],
        }
        for index in range(96)
    ]
    main.write_text("".join(json.dumps(row) + "\n" for row in scenarios[:72]), encoding="utf-8")
    edge.write_text("".join(json.dumps(row) + "\n" for row in scenarios[72:]), encoding="utf-8")
    gold = tmp_path / "reference_set.jsonl"
    gold.write_text("", encoding="utf-8")
    manual = tmp_path / "manual.md"
    manual.write_text("v1.0\n", encoding="utf-8")
    gate_b_manifest = tmp_path / "gate_b_analysis_manifest.json"
    gate_b_manifest.write_text("{}\n", encoding="utf-8")
    reference_manifest = tmp_path / "reference_set_manifest.json"
    reference_manifest.write_text(
        json.dumps(
            {
                "created_at": "2026-09-23T00:00:00+00:00",
                "source_analysis_manifest_sha256": "0" * 64,
                "annotation_fileset_sha256": "1" * 64,
                "assignment_fileset_sha256": "2" * 64,
                "manual_sha256": "3" * 64,
                "scenario_fileset_sha256": sha256_fileset(
                    [main, edge], repository_root=tmp_path
                ),
                "reference_set_sha256": sha256_file(gold),
            }
        ),
        encoding="utf-8",
    )
    result = evaluate_representation_freeze(
        gate_a_path=tmp_path / "gate_a.json",
        gate_b_path=tmp_path / "gate_b.json",
        manual_path=manual,
        main_scenarios_path=main,
        edge_scenarios_path=edge,
        gold_path=gold,
        revision_log_path=tmp_path / "revisions.jsonl",
        gate_a_analysis_manifest_path=tmp_path / "gate_a_analysis_manifest.json",
        gate_b_analysis_manifest_path=gate_b_manifest,
        reference_set_manifest_path=reference_manifest,
        repository_root=tmp_path,
    )

    check = next(item for item in result.checks if item.name == "adjudicated_reference_set")
    assert not check.passed
    assert "source_analysis_matches_gate_b=False" in check.detail


def test_gate_b_uses_versioned_semantic_thresholds() -> None:
    settings = {
        "eligibility_minima_status": "APPROVED_AFTER_FORMAL_BANK_REVIEW",
        "semantic_misunderstanding_max_rates": {"TaskHelpAsSupportErrorRate": 0.05},
        "semantic_misunderstanding_min_eligible_counts": {"TaskHelpAsSupportErrorRate": 1},
    }
    acceptable = {
        "TaskHelpAsSupportErrorRate": {"eligible_count": 1, "rate": 0.04},
    }
    passed, exceeded = evaluate_violation_thresholds(acceptable, settings)
    assert passed
    assert exceeded == {}

    rejected = {
        "TaskHelpAsSupportErrorRate": {"eligible_count": 1, "rate": 0.06},
    }
    passed, exceeded = evaluate_violation_thresholds(rejected, settings)
    assert not passed
    assert exceeded["TaskHelpAsSupportErrorRate"]["category"] == "SEMANTIC_MISUNDERSTANDING"


def test_zero_eligible_opportunities_cannot_pass_with_zero_error_rate() -> None:
    rates = violation_rates([], [], {}, {})
    metrics = set(rates)
    settings = {
        "eligibility_minima_status": "APPROVED_AFTER_FORMAL_BANK_REVIEW",
        "semantic_misunderstanding_max_rates": {metric: 0.05 for metric in metrics},
        "semantic_misunderstanding_min_eligible_counts": {metric: 1 for metric in metrics},
    }

    passed, failures = evaluate_violation_thresholds(rates, settings)

    assert not passed
    assert set(failures) == metrics
    assert all(item["category"] == "INSUFFICIENT_COVERAGE" for item in failures.values())
    assert all(item["eligible_count"] == 0 and item["minimum"] == 1 for item in failures.values())


def test_gate_b_rejects_provisional_minima_even_with_opportunity_coverage() -> None:
    settings = {
        "eligibility_minima_status": "PROVISIONAL_REQUIRES_FORMAL_BANK_REVIEW",
        "semantic_misunderstanding_max_rates": {"TaskHelpAsSupportErrorRate": 0.05},
        "semantic_misunderstanding_min_eligible_counts": {"TaskHelpAsSupportErrorRate": 1},
    }
    rates = {"TaskHelpAsSupportErrorRate": {"eligible_count": 1, "rate": 0.0}}

    passed, failures = evaluate_violation_thresholds(rates, settings)

    assert not passed
    assert failures["ELIGIBILITY_MINIMA_POLICY"]["category"] == "INSUFFICIENT_COVERAGE"


def test_gate_b_rejects_missing_or_nonfinite_semantic_metrics() -> None:
    settings = {
        "eligibility_minima_status": "APPROVED_AFTER_FORMAL_BANK_REVIEW",
        "semantic_misunderstanding_max_rates": {"TaskHelpAsSupportErrorRate": 0.05},
        "semantic_misunderstanding_min_eligible_counts": {"TaskHelpAsSupportErrorRate": 1},
    }
    for rates in (
        {"TaskHelpAsSupportErrorRate": {"rate": 0.0}},
        {"TaskHelpAsSupportErrorRate": {"eligible_count": 1, "rate": float("nan")}},
    ):
        passed, failures = evaluate_violation_thresholds(rates, settings)
        assert not passed
        assert failures["TaskHelpAsSupportErrorRate"]["category"] == "INVALID_METRIC"


def test_gate_b_checks_only_metric_contract_and_key_field_pairability(tmp_path: Path) -> None:
    metrics_path = tmp_path / "field_metrics.csv"
    rows = [
        {"field": field, "freeze_status": "FACT_REVIEW", "pairable_units": "1"}
        for field in sorted(MANUAL_GATE_CONSTRUCTS)
    ]
    rows.append({"field": "NON_KEY_FIELD", "freeze_status": "FACT_REVIEW", "pairable_units": "0"})
    metrics_path.write_text(
        "field,freeze_status,pairable_units\n"
        + "".join(
            f"{row['field']},{row['freeze_status']},{row['pairable_units']}\n"
            for row in rows
        ),
        encoding="utf-8",
    )

    passed, detail = _check_field_metrics(metrics_path)

    assert passed
    assert "rater_coverage_errors" not in detail


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ("missing_column", "missing required columns"),
        ("missing_key_field", "missing_key_fields"),
        ("zero_key_pairability", "key_fields_without_pairable_units"),
        ("blocked_field", "blocked_fields"),
    ],
)
def test_gate_b_rejects_unavailable_or_blocked_field_metrics(
    tmp_path: Path, change: str, expected: str
) -> None:
    metrics_path = tmp_path / "field_metrics.csv"
    rows = [
        {"field": field, "freeze_status": "FACT_REVIEW", "pairable_units": "1"}
        for field in sorted(MANUAL_GATE_CONSTRUCTS)
    ]
    if change == "missing_key_field":
        rows = rows[1:]
    elif change == "zero_key_pairability":
        rows[0]["pairable_units"] = "0"
    elif change == "blocked_field":
        rows[0]["freeze_status"] = "INSUFFICIENT_DATA"
    if change == "missing_column":
        metrics_path.write_text(
            "field,freeze_status\n" + "".join(
                f"{row['field']},{row['freeze_status']}\n" for row in rows
            ),
            encoding="utf-8",
        )
    else:
        metrics_path.write_text(
            "field,freeze_status,pairable_units\n" + "".join(
                f"{row['field']},{row['freeze_status']},{row['pairable_units']}\n"
                for row in rows
            ),
            encoding="utf-8",
        )

    passed, detail = _check_field_metrics(metrics_path)

    assert not passed
    assert expected in detail


def test_quality_threshold_settings_define_all_eligible_count_floors() -> None:
    package_root = Path(__file__).parents[1]
    settings_path = package_root / "settings" / "quality_thresholds_v1.json"
    validation = Validator().validate_paths([settings_path], "quality-thresholds")
    settings = json.loads(settings_path.read_text(encoding="utf-8"))

    assert validation.ok, [str(issue) for issue in validation.issues]
    assert settings["settings_version"] == "1.1"
    assert settings["eligibility_minima_status"] == "PROVISIONAL_REQUIRES_FORMAL_BANK_REVIEW"
    assert set(settings["semantic_misunderstanding_max_rates"]) == set(
        settings["semantic_misunderstanding_min_eligible_counts"]
    )
    assert all(value >= 1 for value in settings["semantic_misunderstanding_min_eligible_counts"].values())


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
        repository_root=tmp_path,
    )
    check = next(item for item in result.checks if item.name == "adjudicated_reference_set")
    assert not check.passed
    assert "missing_reference_keys" in check.detail
