import json
from pathlib import Path

from research.scenario_annotation.annotation_catalog import (
    FieldRequirement,
    field_specs,
)
from research.scenario_annotation.annotation_contract import (
    check_annotation_completeness,
    expected_annotation_keys,
)
from research.scenario_annotation.loader import load_json, load_jsonl


PACKAGE = Path(__file__).parents[1]


def test_human_and_ai_share_one_expected_key_contract() -> None:
    scenario = next(
        row
        for row in load_jsonl(PACKAGE / "assignments" / "round_calibration" / "human" / "module_b.jsonl")
        if row["scenario_id"] == "CAL_019"
    )
    expected = expected_annotation_keys(scenario, "B")
    assert expected == {
        ("E_HARD_TASK_LOW_CEXEC", variable)
        for variable in ("C_EXEC", "IMPORTANCE", "C_OUT", "U_PERC", "F_REC")
    }
    partial = {
        "records": [
            {"target_ref": "E_HARD_TASK_LOW_CEXEC", "variable": "C_EXEC", "label": "LOW"}
        ]
    }
    result = check_annotation_completeness(partial, scenario, "B")
    assert not result.ok
    assert len(result.missing_keys) == 4


def test_conditional_field_is_explicitly_activated_by_visible_target() -> None:
    scenario = {
        "focal_events": [{"event_ref": "EVENT"}],
        "current_tasks": [],
        "annotation_targets": [
            {"module": "A", "target_ref": "EVENT", "variables": ["DEADLINE_SCARCITY_BAND"]}
        ],
    }
    assert ("EVENT", "DEADLINE_SCARCITY_BAND") in expected_annotation_keys(scenario, "A")
    scarcity = next(spec for spec in field_specs("A") if spec.variable == "DEADLINE_SCARCITY_BAND")
    assert scarcity.requirement is FieldRequirement.SCENARIO_CONDITIONAL


def _schema_variable_enum(module: str) -> set[str]:
    schema_name = {"A": "event_annotation.schema.json", "B": "appraisal_annotation.schema.json", "C": "bot_annotation.schema.json"}[module]
    schema = load_json(PACKAGE / "schemas" / schema_name)
    if module == "A":
        return set(schema["$defs"]["record"]["properties"]["variable"]["enum"])
    return set(schema["properties"]["records"]["items"]["properties"]["variable"]["enum"])


def test_catalog_variables_match_schema_and_manual() -> None:
    manual = (PACKAGE / "manuals" / "coding_manual_v0.1.md").read_text(encoding="utf-8")
    for module in ("A", "B", "C"):
        variables = {spec.variable for spec in field_specs(module)}
        assert variables == _schema_variable_enum(module)
        assert all(variable in manual for variable in variables)


def _schema_label_options(module: str) -> dict[str, set]:
    schema_name = {"A": "event_annotation.schema.json", "B": "appraisal_annotation.schema.json", "C": "bot_annotation.schema.json"}[module]
    schema = load_json(PACKAGE / "schemas" / schema_name)
    record = schema["$defs"]["record"] if module == "A" else schema["properties"]["records"]["items"]
    global_labels = record["properties"]["label"].get("enum")
    if global_labels is not None:
        return {spec.variable: set(global_labels) for spec in field_specs(module) if spec.options}
    options: dict[str, set] = {}
    for condition in record.get("allOf", []):
        variables = condition["if"]["properties"]["variable"]
        names = [variables["const"]] if "const" in variables else variables["enum"]
        labels = set(condition["then"]["properties"]["label"]["enum"])
        options.update({name: labels for name in names})
    return options


def test_catalog_label_options_match_schema_enums() -> None:
    for module in ("A", "B", "C"):
        schema_options = _schema_label_options(module)
        catalog_options = {
            spec.variable: set(spec.options)
            for spec in field_specs(module)
            if spec.options
        }
        assert catalog_options == schema_options
