import json
import hashlib

import pytest

from research.scenario_annotation.annotation_catalog import field_specs
from research.scenario_annotation.artifact_fingerprint import sha256_file
from research.scenario_annotation.reference_set import build_reference_set


def _scenario() -> dict:
    return {
        "scenario_id": "S1",
        "scenario_version": "1.0",
        "annotation_modules": ["C"],
        "bot_response_units": [{"response_unit_ref": "BOT"}],
    }


def _document(annotator: str, *, disagreement: bool = False) -> dict:
    records = []
    for spec in field_specs("C"):
        label = spec.options[0]
        if disagreement and spec.variable == "SUPPORT_GATE":
            label = "NON_SUPPORTIVE"
        records.append(
            {
                "annotation_id": f"S1:C:BOT:{spec.variable}:{annotator}",
                "scenario_id": "S1",
                "scenario_version": "1.0",
                "annotation_module": "C",
                "target_ref": "BOT",
                "variable": spec.variable,
                "label": label,
                "manual_version": "1.0",
                "annotator_id": annotator,
            }
        )
    return {
        "scenario_id": "S1",
        "annotation_module": "C",
        "annotator_id": annotator,
        "annotation_round": "VALIDATION",
        "manual_version": "1.0",
        "manual_sha256": hashlib.sha256(b"manual v1.0\n").hexdigest(),
        "records": records,
    }


def _assignments_root(tmp_path, annotators=("AI-A", "AI-B", "AI-C", "Human")):
    package_root = tmp_path / "package"
    root = package_root / "assignments" / "round_validation"
    manual_dir = package_root / "manuals"
    manual_dir.mkdir(parents=True, exist_ok=True)
    manual_path = manual_dir / "coding_manual_v1.0.md"
    manual_path.write_bytes(b"manual v1.0\n")
    for annotator in annotators:
        directory = root / annotator.lower().replace("-", "_")
        directory.mkdir(parents=True)
        assignment_files = []
        for module in ("A", "B", "C"):
            path = directory / f"module_{module.lower()}.jsonl"
            rows = [_scenario()] if module == "C" else []
            path.write_text(
                "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
                encoding="utf-8",
            )
            assignment_files.append(
                {
                    "module": module,
                    "path": path.name,
                    "scenario_count": len(rows),
                    "sha256": sha256_file(path),
                }
            )
        (directory / "manifest.json").write_text(
            json.dumps(
                {
                    "assignment_version": "1.0",
                    "annotator_id": annotator,
                    "annotation_round": "VALIDATION",
                    "manual_version": "1.0",
                    "manual_sha256": sha256_file(manual_path),
                    "scenario_version": "1.0",
                    "randomization_seed": 7,
                    "counterbalance_rule": "fixture",
                    "files": assignment_files,
                }
            ),
            encoding="utf-8",
        )
    return root


def test_unanimous_annotations_become_reference_records(tmp_path) -> None:
    documents = [_document(annotator) for annotator in ("AI-A", "AI-B", "AI-C", "Human")]
    records = build_reference_set(
        annotation_documents=documents,
        scenarios=[_scenario()],
        adjudications=[],
        assignments_root=_assignments_root(tmp_path),
    )
    assert len(records) == len(field_specs("C"))
    assert {record["resolution_mode"] for record in records} == {"UNANIMOUS"}
    assert all(record["source_annotator_count"] == 4 for record in records)
    assert all(record["expected_annotator_count"] == 4 for record in records)


def test_disagreement_without_adjudication_blocks_reference_set(tmp_path) -> None:
    documents = [
        _document("AI-A"),
        _document("AI-B"),
        _document("AI-C"),
        _document("Human", disagreement=True),
    ]
    with pytest.raises(ValueError, match="ADJUDICATION_REQUIRED"):
        build_reference_set(
            annotation_documents=documents,
            scenarios=[_scenario()],
            adjudications=[],
            assignments_root=_assignments_root(tmp_path),
        )


def test_disagreement_with_adjudication_becomes_reference_record(tmp_path) -> None:
    documents = [
        _document("AI-A"),
        _document("AI-B"),
        _document("AI-C"),
        _document("Human", disagreement=True),
    ]
    source_ids = [
        document["records"][2]["annotation_id"]
        for document in documents
    ]
    adjudication = {
        "scenario_id": "S1",
        "target_ref": "BOT",
        "variable": "SUPPORT_GATE",
        "gold_label": "NON_SUPPORTIVE",
        "source_annotation_ids": source_ids,
    }
    records = build_reference_set(
        annotation_documents=documents,
        scenarios=[_scenario()],
        adjudications=[adjudication],
        assignments_root=_assignments_root(tmp_path),
    )
    support = next(record for record in records if record["variable"] == "SUPPORT_GATE")
    assert support["resolution_mode"] == "ADJUDICATED"
    assert support["gold_label"] == "NON_SUPPORTIVE"


def test_reference_set_rejects_missing_assigned_annotator(tmp_path) -> None:
    documents = [_document(annotator) for annotator in ("AI-A", "AI-B", "AI-C")]
    with pytest.raises(ValueError, match="SUBMISSION_INCOMPLETE"):
        build_reference_set(
            annotation_documents=documents,
            scenarios=[_scenario()],
            adjudications=[],
            assignments_root=_assignments_root(tmp_path),
        )
