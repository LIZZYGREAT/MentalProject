from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import shutil

import httpx
import pytest

from research.scenario_annotation.drafts import MODULE_VARIABLES, export_annotations
from research.scenario_annotation.artifact_fingerprint import sha256_file
from research.scenario_annotation.loader import load_jsonl
from research.scenario_annotation.ui.server import AnnotationStore, create_app
from research.scenario_annotation.validation import Validator


PACKAGE_ROOT = Path(__file__).parents[1]


async def _request(app, method: str, url: str, **kwargs) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.request(method, url, **kwargs)


def _valid_module_b_payload(scenario: dict) -> dict:
    target_ref = scenario["focal_events"][0]["event_ref"]
    return {
        "scenario_validity": {
            "scenario_valid": "YES",
            "scenario_plausibility": "HIGH",
            "contradiction_present": "NO",
        },
        "records": [
            {
                "target_ref": target_ref,
                "variable": variable,
                "label": "NO_EVIDENCE",
                "evidence_refs": [],
                "evidence_strength": "N/A",
                "scope": "UNKNOWN_SCOPE",
                "unknown_reason": "NOT_MENTIONED",
                "ambiguity_flag": False,
                "annotator_confidence": "HIGH",
            }
            for variable in MODULE_VARIABLES["B"]
        ],
        "flagged_for_review": False,
        "mark_complete": True,
    }


def _single_scenario_assignment(
    tmp_path: Path, scenario: dict, module: str
) -> tuple[Path, Path, Path]:
    """Create a minimal but fully content-bound assignment for an export test."""
    package_root = tmp_path / "export_package"
    assignment_root = package_root / "assignments" / "round_calibration" / "human"
    manual_path = package_root / "manuals" / "coding_manual_v0.1.md"
    manual_path.parent.mkdir(parents=True)
    shutil.copy2(PACKAGE_ROOT / "manuals" / "coding_manual_v0.1.md", manual_path)

    files = []
    selected_assignment = None
    for current_module in ("A", "B", "C"):
        assignment_path = assignment_root / f"module_{current_module.lower()}.jsonl"
        rows = [scenario] if current_module == module else []
        encoded = "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows
        ).encode("utf-8")
        assignment_path.parent.mkdir(parents=True, exist_ok=True)
        assignment_path.write_bytes(encoded)
        files.append(
            {
                "module": current_module,
                "path": assignment_path.name,
                "scenario_count": len(rows),
                "sha256": hashlib.sha256(encoded).hexdigest(),
            }
        )
        if current_module == module:
            selected_assignment = assignment_path

    manifest = {
        "assignment_version": "1.0",
        "annotation_round": "CALIBRATION",
        "annotator_id": "Human",
        "manual_version": "0.1",
        "manual_sha256": sha256_file(manual_path),
        "scenario_version": "0.1",
        "randomization_seed": 1,
        "counterbalance_rule": "single-scenario export fixture",
        "files": files,
    }
    manifest_path = assignment_root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    assert selected_assignment is not None
    return selected_assignment, manifest_path, manual_path


def test_annotation_ui_cannot_access_hidden_metadata(tmp_path: Path) -> None:
    app = create_app(package_root=PACKAGE_ROOT, draft_root=tmp_path / "drafts")
    store = app.state.annotation_store
    assert isinstance(store, AnnotationStore)
    assert all(
        forbidden not in str(path).lower()
        for path in store.allowed_roots
        for forbidden in ("hidden", "ai_a", "ai_b", "ai_c", "gold", "analysis")
    )

    state = asyncio.run(_request(app, "GET", "/api/state"))
    assert state.status_code == 200
    scenario_id = state.json()["modules"]["A"]["items"][0]["scenario_id"]
    response = asyncio.run(_request(app, "GET", f"/api/scenario/A/{scenario_id}"))
    assert response.status_code == 200
    serialized = json.dumps(response.json()).lower()
    for forbidden in (
        "coverage_tags",
        "pair_design",
        "anchor_reference",
        "expected_labels",
        "gold_label",
    ):
        assert forbidden not in serialized
    assert asyncio.run(_request(app, "GET", "/api/scenario/A/not_assigned")).status_code == 404


def test_annotation_ui_saves_schema_valid_draft(tmp_path: Path) -> None:
    draft_root = tmp_path / "drafts"
    app = create_app(package_root=PACKAGE_ROOT, draft_root=draft_root)
    store = app.state.annotation_store
    scenario = store.scenarios["B"]["CAL_019"]

    response = asyncio.run(
        _request(
            app,
            "POST",
            "/api/draft/B/CAL_019",
            json=_valid_module_b_payload(scenario),
        )
    )
    assert response.status_code == 200, response.text
    draft = response.json()
    assert draft["status"] == "COMPLETE"
    assert draft["validation_errors"] == []
    document = draft["annotation_document"]

    validation_path = tmp_path / "module_b.json"
    validation_path.write_text(json.dumps(document), encoding="utf-8")
    result = Validator().validate_paths(
        [validation_path],
        "appraisal-annotation",
        scenarios={"CAL_019": scenario},
        require_scenario_context=True,
    )
    assert result.ok, [str(issue) for issue in result.issues]


def test_completed_human_draft_exports_to_validated_jsonl(tmp_path: Path) -> None:
    draft_root = tmp_path / "drafts"
    store = AnnotationStore(PACKAGE_ROOT, draft_root=draft_root)
    scenario = store.scenarios["B"]["CAL_019"]
    store.save("B", "CAL_019", _valid_module_b_payload(scenario))
    assignment, manifest_path, manual_path = _single_scenario_assignment(tmp_path, scenario, "B")
    output = tmp_path / "formal" / "module_b.jsonl"

    count = export_annotations(
        annotator_id="Human",
        module="B",
        assignment_path=assignment,
        assignment_manifest_path=manifest_path,
        manual_path=manual_path,
        draft_dir=draft_root / "module_b",
        output_path=output,
    )

    assert count == 1
    assert len(load_jsonl(output)) == 1
    assert not list(output.parent.glob("*.validation.jsonl"))


def test_adjudication_mode_not_available_before_analysis() -> None:
    with pytest.raises(RuntimeError, match="analysis is complete"):
        create_app(package_root=PACKAGE_ROOT, mode="adjudication")


def test_adjudication_mode_rejects_unbound_or_incomplete_inputs(tmp_path: Path) -> None:
    root = tmp_path / "package"
    shutil.copytree(
        PACKAGE_ROOT / "assignments" / "round_calibration",
        root / "assignments" / "round_calibration",
    )
    (root / "scenarios").mkdir(parents=True)
    shutil.copy2(
        PACKAGE_ROOT / "scenarios" / "calibration.jsonl",
        root / "scenarios" / "calibration.jsonl",
    )
    (root / "manuals").mkdir(parents=True)
    shutil.copy2(
        PACKAGE_ROOT / "manuals" / "coding_manual_v0.1.md",
        root / "manuals" / "coding_manual_v0.1.md",
    )

    annotations_root = root / "annotations" / "calibration"
    annotations_root.mkdir(parents=True)
    for annotator_id in ("AI-A", "AI-B", "AI-C", "Human"):
        slug = annotator_id.lower().replace("-", "_")
        for module in ("A", "B", "C"):
            assignment_path = (
                root
                / "assignments"
                / "round_calibration"
                / slug
                / f"module_{module.lower()}.jsonl"
            )
            documents = [
                {
                    "scenario_id": scenario["scenario_id"],
                    "annotation_module": module,
                    "annotator_id": annotator_id,
                    "records": [],
                }
                for scenario in load_jsonl(assignment_path)
            ]
            output = annotations_root / slug / f"module_{module.lower()}.jsonl"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(
                "".join(json.dumps(document) + "\n" for document in documents),
                encoding="utf-8",
            )

    analysis_root = root / "analysis" / "outputs" / "calibration"
    analysis_root.mkdir(parents=True)
    for name in (
        "disagreement_queue.jsonl",
        "disagreement_report.md",
        "field_metrics.csv",
        "critical_violations.jsonl",
        "orthogonality.jsonl",
        "scenario_annotation_report.md",
    ):
        (analysis_root / name).write_text("", encoding="utf-8")
    for name in ("confusion_matrices.json", "semantic_violation_rates.json", "artifact_validity.json"):
        (analysis_root / name).write_text("{}\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="analysis"):
        create_app(package_root=root, mode="adjudication")


def test_annotation_session_selects_round_manifest_manual(tmp_path: Path) -> None:
    root = tmp_path / "package"
    source = PACKAGE_ROOT / "assignments" / "round_calibration" / "human"
    target = root / "assignments" / "round_validation" / "human"
    shutil.copytree(source, target)
    manifest_path = target / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["annotation_round"] = "VALIDATION"
    manifest["manual_version"] = "1.0"
    (root / "manuals").mkdir(parents=True)
    manual_path = root / "manuals" / "coding_manual_v1.0.md"
    manual_path.write_text("manual v1.0", encoding="utf-8")
    manifest["manual_sha256"] = sha256_file(manual_path)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    store = AnnotationStore(root, round_name="validation", annotator_id="Human")

    assert store.session.round_name == "validation"
    assert store.session.manual_version == "1.0"
    assert store.manual_text == "manual v1.0"
    assert store.draft_root == (root / "annotations" / "drafts" / "human" / "validation").resolve()


def test_annotation_session_rejects_changed_manual_content(tmp_path: Path) -> None:
    root = tmp_path / "package"
    source = PACKAGE_ROOT / "assignments" / "round_calibration" / "human"
    target = root / "assignments" / "round_calibration" / "human"
    shutil.copytree(source, target)
    manual_path = root / "manuals" / "coding_manual_v0.1.md"
    manual_path.parent.mkdir(parents=True)
    shutil.copy2(PACKAGE_ROOT / "manuals" / "coding_manual_v0.1.md", manual_path)
    manual_path.write_text(manual_path.read_text(encoding="utf-8") + "\nchanged\n", encoding="utf-8")

    with pytest.raises(ValueError, match="manual content hash"):
        AnnotationStore(root, round_name="calibration", annotator_id="Human")


def test_annotation_session_rejects_changed_assignment_content(tmp_path: Path) -> None:
    root = tmp_path / "package"
    source = PACKAGE_ROOT / "assignments" / "round_calibration" / "human"
    target = root / "assignments" / "round_calibration" / "human"
    shutil.copytree(source, target)
    (root / "manuals").mkdir(parents=True)
    shutil.copy2(
        PACKAGE_ROOT / "manuals" / "coding_manual_v0.1.md",
        root / "manuals" / "coding_manual_v0.1.md",
    )
    with (target / "module_a.jsonl").open("a", encoding="utf-8") as assignment_file:
        assignment_file.write("{}\n")

    with pytest.raises(ValueError, match="row count does not match manifest"):
        AnnotationStore(root, round_name="calibration", annotator_id="Human")
