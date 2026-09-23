from copy import deepcopy
import json
from pathlib import Path

from research.scenario_annotation.loader import load_json
from research.scenario_annotation.validation import Validator


FIXTURES = Path(__file__).with_name("fixtures")


def _record(module: str, variable: str, label, target_ref: str, **extra) -> dict:
    value = {
        "annotation_id": f"CAL_001:{module}:{target_ref}:{variable}:AI-A",
        "scenario_id": "CAL_001",
        "scenario_version": "0.1",
        "annotation_module": module,
        "target_ref": target_ref,
        "variable": variable,
        "label": label,
        "evidence_refs": [],
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


def _document(module: str, records: list[dict]) -> dict:
    return {
        "schema_version": "1.0",
        "scenario_id": "CAL_001",
        "annotation_module": module,
        "annotator_id": "AI-A",
        "annotation_round": "CALIBRATION",
        "manual_version": "0.1",
        "manual_sha256": "a" * 64,
        "scenario_validity": {
            "scenario_valid": "YES",
            "scenario_plausibility": "HIGH",
            "contradiction_present": "NO",
        },
        "records": records,
    }


def _validate(tmp_path: Path, scenario: dict, document: dict, artifact_type: str):
    path = tmp_path / "annotation.json"
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    return Validator().validate_paths(
        [path],
        artifact_type,
        scenarios={scenario["scenario_id"]: scenario},
        require_scenario_context=True,
    )


def test_annotation_evidence_ref_must_exist_in_visible_scenario(tmp_path) -> None:
    scenario = load_json(FIXTURES / "valid_scenario.json")
    document = _document(
        "B",
        [
            _record(
                "B",
                "C_EXEC",
                "LOW",
                "EVENT_MATH_0908",
                evidence_refs=["HIDDEN_OR_OTHER_SCENARIO"],
                evidence_span="没去",
                scope="EPISODE",
            )
        ],
    )
    result = _validate(tmp_path, scenario, document, "appraisal-annotation")
    assert any(issue.code == "INVALID_EVIDENCE_REFERENCE" for issue in result.issues)


def test_appraisal_evidence_span_matches_visible_participant_text_after_light_normalization(tmp_path) -> None:
    scenario = deepcopy(load_json(FIXTURES / "valid_scenario.json"))
    scenario["observed_conversation_evidence"][0]["text"] = "我肯定，能在今晚做完！"
    document = _document(
        "B",
        [
            _record(
                "B",
                "C_EXEC",
                "HIGH",
                "EVENT_MATH_0908",
                evidence_refs=["MSG_001"],
                evidence_span="我肯定能在今晚做完",
                scope="EPISODE",
            )
        ],
    )
    result = _validate(tmp_path, scenario, document, "appraisal-annotation")
    assert result.ok, [str(issue) for issue in result.issues]


def test_appraisal_evidence_span_mismatch_is_rejected(tmp_path) -> None:
    scenario = load_json(FIXTURES / "valid_scenario.json")
    document = _document(
        "B",
        [
            _record(
                "B",
                "C_EXEC",
                "LOW",
                "EVENT_MATH_0908",
                evidence_refs=["MSG_001"],
                evidence_span="我完全没有能力完成作业",
                scope="EPISODE",
            )
        ],
    )
    result = _validate(tmp_path, scenario, document, "appraisal-annotation")
    assert any(issue.code == "EVIDENCE_SPAN_MISMATCH" for issue in result.issues)


def test_bot_evidence_cannot_use_context_known_after_send_time(tmp_path) -> None:
    scenario = deepcopy(load_json(FIXTURES / "valid_scenario.json"))
    scenario["bot_response_units"] = [
        {
            "response_unit_ref": "BOT_001",
            "text": "先休息一分钟，我们再看下一步。",
            "sent_at": "2026-09-08T11:40:00+08:00",
            "known_at": "2026-09-08T11:40:00+08:00",
            "source_ref": "bot_log:P01",
        }
    ]
    scenario["source_refs"].append("bot_log:P01")
    document = _document(
        "C",
        [
            _record(
                "C",
                "SUPPORT_GATE",
                "SUPPORTIVE",
                "BOT_001",
                evidence_refs=["MSG_001"],
            )
        ],
    )
    result = _validate(tmp_path, scenario, document, "bot-annotation")
    assert any(issue.code == "FUTURE_SUPPORT_EVIDENCE" for issue in result.issues)


def test_bot_may_cite_its_response_unit_and_prior_context(tmp_path) -> None:
    scenario = deepcopy(load_json(FIXTURES / "valid_scenario.json"))
    scenario["observed_conversation_evidence"][0]["known_at"] = "2026-09-08T11:30:00+08:00"
    scenario["bot_response_units"] = [
        {
            "response_unit_ref": "BOT_001",
            "text": "先休息一分钟，我们再看下一步。",
            "sent_at": "2026-09-08T11:40:00+08:00",
            "known_at": "2026-09-08T11:40:00+08:00",
            "source_ref": "bot_log:P01",
        }
    ]
    scenario["source_refs"].append("bot_log:P01")
    document = _document(
        "C",
        [
            _record(
                "C",
                "SUPPORT_GATE",
                "SUPPORTIVE",
                "BOT_001",
                evidence_refs=["BOT_001", "MSG_001"],
            )
        ],
    )
    result = _validate(tmp_path, scenario, document, "bot-annotation")
    assert result.ok, [str(issue) for issue in result.issues]
