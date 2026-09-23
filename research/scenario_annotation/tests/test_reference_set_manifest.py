import json

import pytest

from research.scenario_annotation.artifact_fingerprint import sha256_file, sha256_fileset
from research.scenario_annotation.reference_set_manifest import build_reference_set_manifest


def test_reference_set_manifest_binds_analysis_annotations_assignments_manual_and_scenarios(tmp_path):
    annotations = tmp_path / "annotations"
    assignments = tmp_path / "assignments"
    annotations.mkdir()
    assignments.mkdir()
    annotation_file = annotations / "a.json"
    annotation_file.write_text('{"records": []}\n', encoding="utf-8")
    assignment_file = assignments / "module_a.jsonl"
    assignment_file.write_text('{"scenario_id":"S1"}\n', encoding="utf-8")
    manifest_file = assignments / "manifest.json"
    manifest_file.write_text("{}\n", encoding="utf-8")
    scenario_file = tmp_path / "scenarios.jsonl"
    scenario_file.write_text('{"scenario_id":"S1"}\n', encoding="utf-8")
    manual_file = tmp_path / "manual.md"
    manual_file.write_text("manual v1\n", encoding="utf-8")
    reference_file = tmp_path / "reference_set.jsonl"
    reference_file.write_text('{"reference_id":"R1"}\n', encoding="utf-8")
    analysis_manifest_file = tmp_path / "analysis_manifest.json"
    analysis_manifest_file.write_text(
        json.dumps(
            {
                "annotation_fileset_sha256": sha256_fileset([annotation_file]),
                "assignment_fileset_sha256": sha256_fileset([assignment_file, manifest_file]),
                "scenario_fileset_sha256": sha256_fileset([scenario_file]),
                "manual_sha256": sha256_file(manual_file),
            }
        ),
        encoding="utf-8",
    )

    manifest = build_reference_set_manifest(
        reference_set_path=reference_file,
        analysis_manifest_path=analysis_manifest_file,
        annotations_dir=annotations,
        assignments_root=assignments,
        manual_path=manual_file,
        scenario_paths=[scenario_file],
    )

    assert manifest["source_analysis_manifest_sha256"] == sha256_file(analysis_manifest_file)
    assert manifest["annotation_fileset_sha256"] == sha256_fileset([annotation_file])
    assert manifest["assignment_fileset_sha256"] == sha256_fileset([assignment_file, manifest_file])
    assert manifest["manual_sha256"] == sha256_file(manual_file)
    assert manifest["scenario_fileset_sha256"] == sha256_fileset([scenario_file])
    assert manifest["reference_set_sha256"] == sha256_file(reference_file)

    annotation_file.write_text('{"records": [1]}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="current annotation files"):
        build_reference_set_manifest(
            reference_set_path=reference_file,
            analysis_manifest_path=analysis_manifest_file,
            annotations_dir=annotations,
            assignments_root=assignments,
            manual_path=manual_file,
            scenario_paths=[scenario_file],
        )
