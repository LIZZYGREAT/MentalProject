import json

from research.scenario_annotation.corpus import build_calibration, write_calibration
from research.scenario_annotation.validation import FORBIDDEN_VISIBLE_KEYS, Validator


def test_calibration_has_24_scenarios_and_four_six_window_packs() -> None:
    artifacts, pairs = build_calibration()
    assert len(artifacts) == 24
    counts: dict[str, int] = {}
    for artifact in artifacts:
        pack_id = artifact.visible["pack_id"]
        counts[pack_id] = counts.get(pack_id, 0) + 1
    assert counts == {f"PACK_{number:02d}": 6 for number in range(1, 5)}
    assert len(pairs) == 8


def test_calibration_meets_minimum_boundary_coverage() -> None:
    artifacts, _ = build_calibration()
    tags = {item.visible["scenario_id"]: set(item.coverage["coverage_tags"]) for item in artifacts}
    course = sum("COURSE" in value for value in tags.values())
    task = sum(bool(value & {"TASK", "OBLIGATION", "PARENT_CHILD"}) for value in tags.values())
    recovery = sum(bool(value & {"RECOVERY", "RECOVERY_TRAP", "RECOVERY_SUGGESTION"}) for value in tags.values())
    appraisal = sum(bool(value & {"C_EXEC", "C_OUT", "IMPORTANCE", "U_PERC", "F_REC"}) for value in tags.values())
    bot = sum("BOT" in value or "BOT_RELEVANCE" in value or "COPING_SUPPORT" in value for value in tags.values())
    assert course >= 6
    assert task >= 5
    assert recovery >= 4
    assert appraisal >= 6
    assert bot >= 3


def test_visible_scenarios_contain_no_hidden_design_keys() -> None:
    artifacts, _ = build_calibration()
    serialized = json.dumps([item.visible for item in artifacts], ensure_ascii=False)
    for key in FORBIDDEN_VISIBLE_KEYS:
        assert f'"{key}"' not in serialized


def test_visible_scenarios_do_not_contain_design_language() -> None:
    artifacts, _ = build_calibration()
    forbidden = {
        "u_context",
        "u_perc",
        "c_exec",
        "c_out",
        "d_pot",
        "r_pot",
        "f_rec",
        "support_gate",
        "expected",
        "minimal pair",
        "same difficulty",
        "objective stakes",
        "用于测试",
    }
    for artifact in artifacts:
        narrative = artifact.visible["focal_window"]["narrative"].casefold()
        leaked = {term for term in forbidden if term in narrative}
        assert not leaked, f"{artifact.visible['scenario_id']} leaks {sorted(leaked)}"


def test_generated_calibration_artifacts_validate(tmp_path) -> None:
    counts = write_calibration(tmp_path)
    assert counts == {"scenarios": 24, "pairs": 8, "anchors": 7}
    validator = Validator()
    checks = (
        ("scenario", tmp_path / "scenarios" / "calibration.jsonl", 24),
        ("coverage-tags", tmp_path / "hidden" / "coverage_tags.jsonl", 24),
        ("pair-design", tmp_path / "hidden" / "pair_design.jsonl", 8),
        ("anchor-reference", tmp_path / "hidden" / "anchor_reference.jsonl", 7),
    )
    for artifact_type, path, count in checks:
        result = validator.validate_paths([path], artifact_type)
        assert result.ok, [str(issue) for issue in result.issues]
        assert result.checked == count


def test_presentation_pairs_are_natural_structured_counterparts() -> None:
    artifacts, pairs = build_calibration()
    scenarios = {item.visible["scenario_id"]: item.visible for item in artifacts}
    presentation_pairs = [pair for pair in pairs if pair["manipulated_factor"] == "presentation_mode"]
    assert len(presentation_pairs) == 4
    for pair in presentation_pairs:
        left, right = (scenarios[scenario_id] for scenario_id in pair["scenario_ids"])
        assert {left["presentation_mode"], right["presentation_mode"]} == {"NATURAL", "STRUCTURED"}
        assert left["focal_events"] == right["focal_events"]
        assert left["observed_conversation_evidence"] == right["observed_conversation_evidence"]
