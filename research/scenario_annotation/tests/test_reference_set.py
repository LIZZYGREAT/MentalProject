import pytest

from research.scenario_annotation.annotation_catalog import field_specs
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
        "records": records,
    }


def test_unanimous_annotations_become_reference_records() -> None:
    documents = [_document(annotator) for annotator in ("AI-A", "AI-B", "AI-C", "Human")]
    records = build_reference_set(
        annotation_documents=documents,
        scenarios=[_scenario()],
        adjudications=[],
    )
    assert len(records) == len(field_specs("C"))
    assert {record["resolution_mode"] for record in records} == {"UNANIMOUS"}


def test_disagreement_without_adjudication_blocks_reference_set() -> None:
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
        )


def test_disagreement_with_adjudication_becomes_reference_record() -> None:
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
    )
    support = next(record for record in records if record["variable"] == "SUPPORT_GATE")
    assert support["resolution_mode"] == "ADJUDICATED"
    assert support["gold_label"] == "NON_SUPPORTIVE"
