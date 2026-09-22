from pathlib import Path

from research.scenario_annotation.validation import Validator


FIXTURES = Path(__file__).with_name("fixtures")


def test_valid_scenario_passes_schema_and_semantics() -> None:
    result = Validator().validate_paths([FIXTURES / "valid_scenario.json"], "scenario")
    assert result.ok, [str(issue) for issue in result.issues]
    assert result.checked == 1


def test_invalid_scenario_reports_hidden_metadata() -> None:
    result = Validator().validate_paths([FIXTURES / "invalid_hidden_scenario.json"], "scenario")
    assert not result.ok
    assert any(issue.code == "HIDDEN_METADATA" for issue in result.issues)


def test_future_known_information_is_rejected() -> None:
    scenario = (FIXTURES / "valid_scenario.json").read_text(encoding="utf-8")
    future = scenario.replace(
        '"known_at": "2026-09-08T11:45:00+08:00"',
        '"known_at": "2026-09-08T12:05:00+08:00"',
        1,
    )
    path = FIXTURES / "_future_tmp.json"
    try:
        path.write_text(future, encoding="utf-8")
        result = Validator().validate_paths([path], "scenario")
    finally:
        path.unlink(missing_ok=True)
    assert any(issue.code == "FUTURE_KNOWLEDGE" for issue in result.issues)
