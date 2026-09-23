import json
from pathlib import Path
import shutil

import pytest

from research.scenario_annotation.analysis.common import AnnotationRow
from research.scenario_annotation.analysis.critical_violations import find_critical_violations, violation_rates
from research.scenario_annotation.analysis.artifact_validity import evaluate_artifact_validity
from research.scenario_annotation.analysis.anchors import analyze_anchors, anchor_summary
from research.scenario_annotation.analysis.orthogonality import analyze_orthogonality, orthogonality_summary
from research.scenario_annotation.analysis.pipeline import run_analysis
from research.scenario_annotation.analysis_manifest import verify_analysis_manifest_current
from research.scenario_annotation.assignments import build_assignments
from research.scenario_annotation.artifact_fingerprint import sha256_file
from research.scenario_annotation.corpus import write_calibration
from research.scenario_annotation.loader import load_jsonl


PACKAGE_ROOT = Path(__file__).parents[1]


def _prepare_analysis_repository(repository_root: Path) -> Path:
    package_root = repository_root / "research" / "scenario_annotation"
    package_root.mkdir(parents=True)
    shutil.copytree(PACKAGE_ROOT / "analysis", package_root / "analysis")
    shutil.copytree(PACKAGE_ROOT / "schemas", package_root / "schemas")
    for relative in (
        "analysis_manifest.py",
        "artifact_fingerprint.py",
        "annotation_catalog.py",
        "annotation_contract.py",
        "assignments.py",
        "gates.py",
        "validation.py",
    ):
        shutil.copy2(PACKAGE_ROOT / relative, package_root / relative)
    for folder in ("manuals", "settings"):
        shutil.copytree(PACKAGE_ROOT / folder, package_root / folder)
    (package_root / "adjudication").mkdir()
    return package_root


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


def test_design_anchors_are_audited_and_mismatches_enter_manual_review() -> None:
    anchors = [
        {
            "anchor_id": "A_PARTIAL",
            "scenario_id": "S_PARTIAL",
            "target_ref": "E_PARTIAL",
            "variable": "PARTIAL_ENCODING_BASIS",
            "target_variable": "EXECUTION_EXPOSURE",
            "record_attribute": "partial_encoding_basis",
            "intended_label": "ACTUAL_INTERVAL",
            "is_gold": False,
        },
        {
            "anchor_id": "A_LIFECYCLE",
            "scenario_id": "S_SKIP",
            "target_ref": "E_SKIP",
            "variable": "LIFECYCLE",
            "intended_label": "SKIPPED",
            "is_gold": False,
        },
    ]
    rows = [
        _row(
            "S_PARTIAL", "AI-A", "EXECUTION_EXPOSURE", "PARTIAL", "E_PARTIAL", "A",
            partial_encoding_basis="ACTUAL_INTERVAL",
        ),
        _row("S_SKIP", "AI-B", "LIFECYCLE", "ATTENDED", "E_SKIP", "A"),
    ]
    expected = {"S_PARTIAL": {"AI-A"}, "S_SKIP": {"AI-B"}}

    audit = analyze_anchors(anchors, rows, expected_annotators=expected)
    summary = anchor_summary(audit)

    assert [item["status"] for item in audit] == ["MATCH", "MISMATCH_REVIEW_REQUIRED"]
    assert audit[1]["review_status"] == "REVIEW_REQUIRED"
    assert summary["expected_anchors"] == 2
    assert summary["evaluated_checks"] == 2
    assert summary["unexplained_mismatches"] == 1

    reviewed = analyze_anchors(
        anchors,
        rows,
        expected_annotators=expected,
        review_decisions={
            ("A_LIFECYCLE", "AI-B"): {
                "disposition": "VALID_ALTERNATIVE",
                "reviewer_id": "Human",
                "rationale": "The available evidence permits this alternative reading.",
            }
        },
    )
    assert reviewed[1]["status"] == "MISMATCH_RESOLVED"
    assert anchor_summary(reviewed)["unexplained_mismatches"] == 0


def test_anchor_audit_requires_every_assigned_annotator_record() -> None:
    anchor = {
        "anchor_id": "A_SKIP",
        "scenario_id": "S_SKIP",
        "target_ref": "E_SKIP",
        "variable": "LIFECYCLE",
        "intended_label": "SKIPPED",
        "is_gold": False,
    }
    rows = [_row("S_SKIP", "AI-A", "LIFECYCLE", "SKIPPED", "E_SKIP", "A")]
    audit = analyze_anchors(
        [anchor],
        rows,
        expected_annotators={"S_SKIP": {"AI-A", "AI-B"}},
    )

    assert {item["status"] for item in audit} == {"MATCH", "NOT_COMPARABLE"}
    summary = anchor_summary(audit)
    assert summary["expected_checks"] == 2
    assert summary["evaluated_checks"] == 1
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
        "manual_sha256": sha256_file(PACKAGE_ROOT / "manuals" / "coding_manual_v0.1.md"),
        "scenario_validity": {
            "scenario_valid": "YES",
            "scenario_plausibility": "HIGH",
            "contradiction_present": "NO",
        },
        "records": [record],
    }


def test_full_analysis_pipeline_writes_all_required_outputs(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "research.scenario_annotation.analysis_manifest._git_revision",
        lambda _: "test-revision",
    )
    repository_root = tmp_path / "repo"
    package_root = _prepare_analysis_repository(repository_root)
    corpus_root = package_root
    write_calibration(corpus_root)
    annotations = package_root / "annotations" / "calibration"
    annotations.mkdir(parents=True)
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
    output = package_root / "analysis" / "outputs" / "calibration"
    summary = run_analysis(
        annotations_dir=annotations,
        scenarios_path=corpus_root / "scenarios" / "calibration.jsonl",
        coverage_path=corpus_root / "hidden" / "coverage_tags.jsonl",
        pairs_path=corpus_root / "hidden" / "pair_design.jsonl",
        output_dir=output,
        quality_thresholds_path=package_root / "settings" / "quality_thresholds_v1.json",
        repository_root=repository_root,
        anchor_review_decisions_path=(
            package_root / "adjudication" / "anchor_review_decisions.jsonl"
        ),
        manual_path=package_root / "manuals" / "coding_manual_v0.1.md",
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
        "anchor_audit.jsonl",
        "anchor_summary.json",
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
    generated_validity = json.loads((output / "artifact_validity.json").read_text(encoding="utf-8"))
    assert generated_validity["status"] == "FAIL"
    assert generated_validity["checks"]["assignment_submission_integrity"] == "NOT_EVALUATED"

    manifest_path = output / "analysis_manifest.json"
    manifest_text = manifest_path.read_text(encoding="utf-8")
    assert str(repository_root) not in manifest_text
    manifest = json.loads(manifest_text)
    assert manifest["analysis_version"] == "1.2"
    assert all("logical_path" in entry and "path" not in entry for entry in manifest["scenario_files"])
    assert manifest["analysis_code_sha256"]

    relocated_root = tmp_path / "relocated_repo"
    shutil.copytree(repository_root, relocated_root)
    relocated_package = relocated_root / "research" / "scenario_annotation"
    relocated_manifest = relocated_package / "analysis" / "outputs" / "calibration" / "analysis_manifest.json"
    verify_analysis_manifest_current(
        relocated_manifest,
        annotations_dir=relocated_package / "annotations" / "calibration",
        scenario_paths=[relocated_package / "scenarios" / "calibration.jsonl"],
        coverage_path=relocated_package / "hidden" / "coverage_tags.jsonl",
        pair_design_path=relocated_package / "hidden" / "pair_design.jsonl",
        quality_thresholds_path=relocated_package / "settings" / "quality_thresholds_v1.json",
        anchor_reference_path=relocated_package / "hidden" / "anchor_reference.jsonl",
        anchor_review_decisions_path=(
            relocated_package / "adjudication" / "anchor_review_decisions.jsonl"
        ),
        manual_path=relocated_package / "manuals" / "coding_manual_v0.1.md",
        repository_root=relocated_root,
    )
    relocated_manifest_data = json.loads(relocated_manifest.read_text(encoding="utf-8"))
    relocated_manifest_data["analysis_version"] = "0.9"
    relocated_manifest.write_text(json.dumps(relocated_manifest_data), encoding="utf-8")
    with pytest.raises(ValueError, match="analysis version is stale"):
        verify_analysis_manifest_current(
            relocated_manifest,
            annotations_dir=relocated_package / "annotations" / "calibration",
            scenario_paths=[relocated_package / "scenarios" / "calibration.jsonl"],
            coverage_path=relocated_package / "hidden" / "coverage_tags.jsonl",
            pair_design_path=relocated_package / "hidden" / "pair_design.jsonl",
            quality_thresholds_path=relocated_package / "settings" / "quality_thresholds_v1.json",
            anchor_reference_path=relocated_package / "hidden" / "anchor_reference.jsonl",
            anchor_review_decisions_path=(
                relocated_package / "adjudication" / "anchor_review_decisions.jsonl"
            ),
            manual_path=relocated_package / "manuals" / "coding_manual_v0.1.md",
            repository_root=relocated_root,
        )
    relocated_manifest_data["analysis_version"] = manifest["analysis_version"]
    relocated_manifest.write_text(json.dumps(relocated_manifest_data), encoding="utf-8")
    agreement_code = relocated_package / "analysis" / "agreement.py"
    agreement_code.write_text(
        agreement_code.read_text(encoding="utf-8") + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="analysis implementation changed"):
        verify_analysis_manifest_current(
            relocated_manifest,
            annotations_dir=relocated_package / "annotations" / "calibration",
            scenario_paths=[relocated_package / "scenarios" / "calibration.jsonl"],
            coverage_path=relocated_package / "hidden" / "coverage_tags.jsonl",
            pair_design_path=relocated_package / "hidden" / "pair_design.jsonl",
            quality_thresholds_path=relocated_package / "settings" / "quality_thresholds_v1.json",
            anchor_reference_path=relocated_package / "hidden" / "anchor_reference.jsonl",
            anchor_review_decisions_path=(
                relocated_package / "adjudication" / "anchor_review_decisions.jsonl"
            ),
            manual_path=relocated_package / "manuals" / "coding_manual_v0.1.md",
            repository_root=relocated_root,
        )

    anchor_reference_path = corpus_root / "hidden" / "anchor_reference.jsonl"
    anchor_reference_path.write_text(anchor_reference_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="stale analysis manifest"):
        verify_analysis_manifest_current(
            output / "analysis_manifest.json",
            annotations_dir=annotations,
            scenario_paths=[corpus_root / "scenarios" / "calibration.jsonl"],
            coverage_path=corpus_root / "hidden" / "coverage_tags.jsonl",
            pair_design_path=corpus_root / "hidden" / "pair_design.jsonl",
            quality_thresholds_path=package_root / "settings" / "quality_thresholds_v1.json",
            anchor_review_decisions_path=(
                package_root / "adjudication" / "anchor_review_decisions.jsonl"
            ),
            manual_path=package_root / "manuals" / "coding_manual_v0.1.md",
            repository_root=repository_root,
        )

    annotation_path = annotations / "annotation_0.json"
    annotation_path.write_text(annotation_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="stale analysis manifest"):
        verify_analysis_manifest_current(
            output / "analysis_manifest.json",
            annotations_dir=annotations,
            scenario_paths=[corpus_root / "scenarios" / "calibration.jsonl"],
            coverage_path=corpus_root / "hidden" / "coverage_tags.jsonl",
            pair_design_path=corpus_root / "hidden" / "pair_design.jsonl",
            quality_thresholds_path=package_root / "settings" / "quality_thresholds_v1.json",
            anchor_review_decisions_path=(
                package_root / "adjudication" / "anchor_review_decisions.jsonl"
            ),
            manual_path=package_root / "manuals" / "coding_manual_v0.1.md",
            repository_root=repository_root,
        )


def test_artifact_validity_fails_for_invalid_scenario(tmp_path) -> None:
    corpus_root = tmp_path / "corpus"
    write_calibration(corpus_root)
    scenario_path = corpus_root / "scenarios" / "calibration.jsonl"
    scenarios = load_jsonl(scenario_path)
    scenarios[0]["participant_context"]["known_at"] = "2026-09-30T08:00:00+08:00"
    scenario_path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in scenarios),
        encoding="utf-8",
    )
    annotations = tmp_path / "annotations"
    annotations.mkdir()

    result = evaluate_artifact_validity(
        scenarios_paths=[scenario_path],
        annotations_dir=annotations,
        coverage_path=corpus_root / "hidden" / "coverage_tags.jsonl",
        pairs_path=corpus_root / "hidden" / "pair_design.jsonl",
    )

    assert result["status"] == "FAIL"
    assert result["checks"]["scenario_validator"] == "FAIL"
    assert result["checks"]["known_at_checks"] == "FAIL"


def test_analysis_writes_failed_validity_for_hidden_metadata_leak(tmp_path) -> None:
    corpus_root = tmp_path / "corpus"
    write_calibration(corpus_root)
    scenario_path = corpus_root / "scenarios" / "calibration.jsonl"
    scenarios = load_jsonl(scenario_path)
    scenarios[0]["coverage_tags"] = ["HIGH_LOAD"]
    scenario_path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in scenarios),
        encoding="utf-8",
    )
    annotations = tmp_path / "annotations"
    annotations.mkdir()
    output = tmp_path / "output"

    with pytest.raises(ValueError, match="artifact validation failed"):
        run_analysis(
            annotations_dir=annotations,
            scenarios_path=scenario_path,
            coverage_path=corpus_root / "hidden" / "coverage_tags.jsonl",
            pairs_path=corpus_root / "hidden" / "pair_design.jsonl",
            output_dir=output,
        )

    validity = json.loads((output / "artifact_validity.json").read_text(encoding="utf-8"))
    assert validity["status"] == "FAIL"
    assert validity["checks"]["hidden_metadata"] == "FAIL"


def test_artifact_validity_checks_assignment_file_integrity(tmp_path) -> None:
    corpus_root = tmp_path / "corpus"
    write_calibration(corpus_root)
    package_root = tmp_path / "package"
    assignments = package_root / "assignments" / "round_calibration"
    manual_path = package_root / "manuals" / "coding_manual_v0.1.md"
    manual_path.parent.mkdir(parents=True)
    manual_path.write_bytes((PACKAGE_ROOT / "manuals" / "coding_manual_v0.1.md").read_bytes())
    build_assignments(
        corpus_root / "scenarios" / "calibration.jsonl",
        corpus_root / "hidden" / "pair_design.jsonl",
        assignments,
        annotation_round="CALIBRATION",
        manual_version="0.1",
        manual_path=manual_path,
        scenario_version="0.1",
        seed=12001,
    )
    assignment_file = assignments / "ai_a" / "module_a.jsonl"
    assignment_file.write_text(assignment_file.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    annotations = tmp_path / "annotations"
    annotations.mkdir()

    result = evaluate_artifact_validity(
        scenarios_paths=[corpus_root / "scenarios" / "calibration.jsonl"],
        annotations_dir=annotations,
        coverage_path=corpus_root / "hidden" / "coverage_tags.jsonl",
        pairs_path=corpus_root / "hidden" / "pair_design.jsonl",
        assignments_root=assignments,
    )

    assert result["checks"]["assignment_submission_integrity"] == "FAIL"
    issues = result["details"]["assignment_submission_integrity"]["issues"]
    assert any(
        "assignment file hash does not match manifest" in issue["message"]
        for issue in issues
    )
