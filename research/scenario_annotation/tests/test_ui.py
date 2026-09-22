from __future__ import annotations

import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from research.scenario_annotation.drafts import MODULE_VARIABLES, export_annotations
from research.scenario_annotation.loader import load_jsonl
from research.scenario_annotation.ui.server import AnnotationStore, create_app
from research.scenario_annotation.validation import Validator


PACKAGE_ROOT = Path(__file__).parents[1]


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


def test_annotation_ui_cannot_access_hidden_metadata(tmp_path: Path) -> None:
    app = create_app(package_root=PACKAGE_ROOT, draft_root=tmp_path / "drafts")
    store = app.state.annotation_store
    assert isinstance(store, AnnotationStore)
    assert all(
        forbidden not in str(path).lower()
        for path in store.allowed_roots
        for forbidden in ("hidden", "ai_a", "ai_b", "ai_c", "gold", "analysis")
    )

    with TestClient(app) as client:
        state = client.get("/api/state")
        assert state.status_code == 200
        scenario_id = state.json()["modules"]["A"]["items"][0]["scenario_id"]
        response = client.get(f"/api/scenario/A/{scenario_id}")
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
        assert client.get("/api/scenario/A/not_assigned").status_code == 404


def test_annotation_ui_saves_schema_valid_draft(tmp_path: Path) -> None:
    draft_root = tmp_path / "drafts"
    app = create_app(package_root=PACKAGE_ROOT, draft_root=draft_root)
    store = app.state.annotation_store
    scenario = store.scenarios["B"]["CAL_019"]

    with TestClient(app) as client:
        response = client.post(
            "/api/draft/B/CAL_019", json=_valid_module_b_payload(scenario)
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
    assignment = tmp_path / "assignment.jsonl"
    assignment.write_text(
        json.dumps(scenario, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8"
    )
    output = tmp_path / "formal" / "module_b.jsonl"

    count = export_annotations(
        annotator_id="Human",
        module="B",
        assignment_path=assignment,
        draft_dir=draft_root / "module_b",
        output_path=output,
    )

    assert count == 1
    assert len(load_jsonl(output)) == 1
    assert not list(output.parent.glob("*.validation.jsonl"))


def test_adjudication_mode_not_available_before_analysis() -> None:
    with pytest.raises(RuntimeError, match="analysis is complete"):
        create_app(package_root=PACKAGE_ROOT, mode="adjudication")
