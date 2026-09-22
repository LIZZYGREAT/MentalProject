import json

import pytest

from research.scenario_annotation.loader import ArtifactLoadError, Scenario, load_jsonl


def test_jsonl_loader_reports_line_number(tmp_path) -> None:
    path = tmp_path / "broken.jsonl"
    path.write_text('{"ok": true}\nnot-json\n', encoding="utf-8")
    with pytest.raises(ArtifactLoadError, match=r"broken\.jsonl:2"):
        load_jsonl(path)


def test_scenario_typed_loader_preserves_payload() -> None:
    value = {
        "scenario_id": "S1",
        "scenario_version": "0.1",
        "pack_id": "PACK_01",
        "participant_id": "P01",
        "presentation_mode": "NATURAL",
        "annotation_time": "2026-09-22T00:00:00Z",
        "known_at_cutoff": "2026-09-22T00:00:00Z",
        "extra": 3,
    }
    scenario = Scenario.from_mapping(json.loads(json.dumps(value)))
    assert scenario.scenario_id == "S1"
    assert scenario.payload["extra"] == 3
