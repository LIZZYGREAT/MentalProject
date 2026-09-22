from copy import deepcopy
import json
from pathlib import Path

import pytest

from research.scenario_annotation.validation import Validator


BASE = {
    "schema_version": "1.0",
    "scenario_id": "CAL_001",
    "annotator_id": "AI-A",
    "annotation_round": "CALIBRATION",
    "manual_version": "0.1",
    "scenario_validity": {
        "scenario_valid": "YES",
        "scenario_plausibility": "HIGH",
        "contradiction_present": "NO",
    },
}


def record(module: str, variable: str, label, **extra):
    value = {
        "annotation_id": f"CAL_001:{module}:{variable}:AI-A",
        "scenario_id": "CAL_001",
        "scenario_version": "0.1",
        "annotation_module": module,
        "target_ref": "EVENT_MATH_0908",
        "variable": variable,
        "label": label,
        "evidence_refs": ["MSG_001"],
        "evidence_strength": "STRONG",
        "ambiguity_flag": False,
        "annotator_confidence": "HIGH",
        "manual_version": "0.1",
        "annotator_id": "AI-A",
        "annotation_round": "CALIBRATION",
        "created_at": "2026-09-22T12:00:00+08:00",
    }
    value.update(extra)
    return value


@pytest.mark.parametrize(
    ("module", "artifact_type", "row"),
    [
        ("A", "event-annotation", record("A", "LIFECYCLE", "SKIPPED")),
        ("B", "appraisal-annotation", record("B", "C_EXEC", "LOW", scope="EPISODE", evidence_span="我肯定做不完")),
        ("C", "bot-annotation", record("C", "SUPPORT_GATE", "SUPPORTIVE")),
    ],
)
def test_valid_module_documents(tmp_path: Path, module: str, artifact_type: str, row: dict) -> None:
    document = deepcopy(BASE)
    document.update({"annotation_module": module, "records": [row]})
    path = tmp_path / "annotation.json"
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    result = Validator().validate_paths([path], artifact_type)
    assert result.ok, [str(issue) for issue in result.issues]


def test_invalid_label_fails_clearly(tmp_path: Path) -> None:
    document = deepcopy(BASE)
    document.update(
        {
            "annotation_module": "B",
            "records": [record("B", "C_EXEC", "VERY_HIGH", scope="EPISODE", evidence_span="很难")],
        }
    )
    path = tmp_path / "annotation.json"
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    result = Validator().validate_paths([path], "appraisal-annotation")
    assert not result.ok
    assert any("VERY_HIGH" in issue.message for issue in result.issues)


def test_no_evidence_requires_unknown_reason(tmp_path: Path) -> None:
    document = deepcopy(BASE)
    row = record("B", "C_EXEC", "NO_EVIDENCE", scope="UNKNOWN_SCOPE", evidence_strength="N/A", evidence_refs=[])
    document.update({"annotation_module": "B", "records": [row]})
    path = tmp_path / "annotation.json"
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    result = Validator().validate_paths([path], "appraisal-annotation")
    assert any(issue.code == "MISSING_UNKNOWN_REASON" for issue in result.issues)
