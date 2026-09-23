"""UI field controls derived from the frozen calibration schema vocabulary."""

from __future__ import annotations

from typing import Any, Mapping

from ..annotation_catalog import FieldRequirement, field_specs


def module_field_specs(module: str, scenario: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    activated = {
        str(variable)
        for target in (scenario or {}).get("annotation_targets", [])
        if isinstance(target, Mapping) and target.get("module") == module
        for variable in target.get("variables", [])
    }
    values: list[dict[str, Any]] = []
    for spec in field_specs(module):
        if (
            spec.requirement is FieldRequirement.SCENARIO_CONDITIONAL
            and spec.variable not in activated
        ):
            continue
        value: dict[str, Any] = {
            "variable": spec.variable,
            "kind": spec.value_kind,
            "requirement": spec.requirement.value,
        }
        if spec.options:
            value["options"] = list(spec.options)
        if spec.special_options:
            value["special_options"] = list(spec.special_options)
        if spec.placeholder:
            value["placeholder"] = spec.placeholder
        values.append(value)
    return values
