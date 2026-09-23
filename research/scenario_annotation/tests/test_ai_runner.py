import json
from pathlib import Path

import pytest

from research.scenario_annotation.ai_runner.contracts import RetryReason, retry_allowed
from research.scenario_annotation.ai_runner.importer import import_ai_output
from research.scenario_annotation.ai_runner.packets import export_ai_packets
from research.scenario_annotation.ai_runner.packets import _manual_excerpt
from research.scenario_annotation.loader import load_json, load_jsonl
from research.scenario_annotation.validation import FORBIDDEN_VISIBLE_KEYS, Validator


PACKAGE = Path(__file__).parents[1]
ASSIGNMENT = PACKAGE / "assignments" / "round_calibration" / "ai_a"


def test_exported_packet_contains_only_one_visible_scenario_and_relevant_manual_section(tmp_path) -> None:
    summary = export_ai_packets(
        annotator_id="AI-A",
        annotation_round="CALIBRATION",
        module="B",
        assignment_path=ASSIGNMENT / "module_b.jsonl",
        assignment_manifest_path=ASSIGNMENT / "manifest.json",
        manual_path=PACKAGE / "manuals" / "coding_manual_v0.1.md",
        output_schema_path=PACKAGE / "schemas" / "ai_output.schema.json",
        output_dir=tmp_path,
    )
    assert summary["scenario_count"] == 9
    packet = load_json(tmp_path / "CAL_019.json")
    assert packet["scenario_id"] == "CAL_019"
    assert packet["annotation_module"] == "B"
    assert "## 4. Module B" in packet["manual_excerpt"]
    assert "## 3. Module A" not in packet["manual_excerpt"]
    serialized_view = json.dumps(packet["scenario_view"], ensure_ascii=False)
    for key in FORBIDDEN_VISIBLE_KEYS:
        assert f'"{key}"' not in serialized_view
    assert "gold_label" not in serialized_view


def test_manual_heading_changes_do_not_break_module_extraction() -> None:
    manual = (PACKAGE / "manuals" / "coding_manual_v0.1.md").read_text(encoding="utf-8")
    renamed = manual.replace("## 4. Module B", "## 四、个人评估模块")
    excerpt = _manual_excerpt(renamed, "B")
    assert "## 四、个人评估模块" in excerpt
    assert "Module C" not in excerpt


def _valid_raw_output() -> dict:
    value = {
        "scenario_validity": {
            "scenario_valid": "YES",
            "scenario_plausibility": "HIGH",
            "contradiction_present": "NO",
        },
        "records": [
            {
                "variable": "C_EXEC",
                "target_ref": "E_HARD_TASK_LOW_CEXEC",
                "label": "LOW",
                "evidence_refs": ["MSG_019"],
                "evidence_span": "我完全不知道怎么下手，今天肯定做不出来",
                "evidence_strength": "STRONG",
                "scope": "EPISODE",
                "unknown_reason": "NOT_MENTIONED",
                "ambiguity_flag": False,
                "annotator_confidence": "HIGH",
                "notes": "直接的 episode-specific 陈述。",
            }
        ],
    }
    for variable in ("IMPORTANCE", "C_OUT", "U_PERC", "F_REC"):
        value["records"].append(
            {
                "variable": variable,
                "target_ref": "E_HARD_TASK_LOW_CEXEC",
                "label": "NO_EVIDENCE",
                "evidence_refs": [],
                "evidence_strength": "N/A",
                "scope": "EPISODE",
                "unknown_reason": "NOT_MENTIONED",
                "ambiguity_flag": False,
                "annotator_confidence": "HIGH",
            }
        )
    return value


def _import(tmp_path: Path, raw: dict) -> Path:
    raw_path = tmp_path / "raw.json"
    raw_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    return import_ai_output(
        annotator_id="AI-A",
        annotation_round="CALIBRATION",
        module="B",
        scenario_id="CAL_019",
        assignment_path=ASSIGNMENT / "module_b.jsonl",
        assignment_manifest_path=ASSIGNMENT / "manifest.json",
        raw_output_path=raw_path,
        raw_output_schema_path=PACKAGE / "schemas" / "ai_output.schema.json",
        output_dir=tmp_path / "drafts",
        provider="provider-a",
        model="model-a",
        temperature=0.0,
        seed=17,
        request_id="req-001",
        attempt=1,
        created_at="2026-09-22T12:00:00+00:00",
    )


def test_importer_owns_system_fields_and_records_provenance(tmp_path) -> None:
    draft_path = _import(tmp_path, _valid_raw_output())
    document = load_json(draft_path)
    record = document["records"][0]
    assert record["annotator_id"] == "AI-A"
    assert record["manual_version"] == "0.1"
    assert record["scenario_version"] == "0.1"
    assert record["annotation_id"] == "CAL_019:B:E_HARD_TASK_LOW_CEXEC:C_EXEC:AI-A"
    assert document["runner_provenance"]["provider"] == "provider-a"
    assert document["runner_provenance"]["attempt"] == 1
    assert len(document["runner_provenance"]["raw_output_sha256"]) == 64
    scenarios = {row["scenario_id"]: row for row in load_jsonl(ASSIGNMENT / "module_b.jsonl")}
    result = Validator().validate_paths(
        [draft_path],
        "appraisal-annotation",
        scenarios=scenarios,
        require_scenario_context=True,
    )
    assert result.ok, [str(issue) for issue in result.issues]


def test_importer_rejects_model_control_of_system_fields(tmp_path) -> None:
    raw = _valid_raw_output()
    raw["annotator_id"] = "Human"
    with pytest.raises(ValueError, match="output contract failure"):
        _import(tmp_path, raw)


def test_ai_partial_annotation_cannot_import_as_complete(tmp_path) -> None:
    raw = _valid_raw_output()
    raw["records"] = raw["records"][:1]
    with pytest.raises(ValueError, match="completeness failure"):
        _import(tmp_path, raw)


def test_retry_policy_allows_transport_or_format_failures_without_label_feedback() -> None:
    assert retry_allowed(RetryReason.NETWORK_FAILURE)
    assert retry_allowed(RetryReason.SCHEMA_FAILURE)
    assert not retry_allowed(RetryReason.SCHEMA_FAILURE, {"gold"})
    assert not retry_allowed(RetryReason.INVALID_JSON, {"human_disagreement"})
