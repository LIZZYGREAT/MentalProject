"""Atomic Human drafts and formal annotation export."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Mapping

from jsonschema import Draft202012Validator, FormatChecker

from .annotation_catalog import field_specs
from .annotation_contract import (
    check_annotation_completeness,
    module_targets,
)
from .loader import load_json, load_jsonl
from .validation import Validator


MODULE_VARIABLES = {
    module: tuple(spec.variable for spec in field_specs(module))
    for module in ("A", "B", "C")
}


def missing_required_records(payload: Mapping[str, Any], scenario: Mapping[str, Any], module: str) -> list[str]:
    result = check_annotation_completeness(payload, scenario, module)
    return [f"{target_ref}:{variable}" for target_ref, variable in result.missing_keys]


def build_annotation_document(
    *,
    payload: Mapping[str, Any],
    scenario: Mapping[str, Any],
    manifest: Mapping[str, Any],
    annotator_id: str,
    annotation_round: str,
    module: str,
    created_at: str,
) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    for raw_record in payload.get("records", []):
        if raw_record.get("label") in (None, ""):
            continue
        target_ref = str(raw_record.get("target_ref", ""))
        variable = str(raw_record.get("variable", ""))
        record = dict(raw_record)
        record.update(
            {
                "annotation_id": f"{scenario['scenario_id']}:{module}:{target_ref}:{variable}:{annotator_id}",
                "scenario_id": scenario["scenario_id"],
                "scenario_version": scenario["scenario_version"],
                "annotation_module": module,
                "manual_version": manifest["manual_version"],
                "annotator_id": annotator_id,
                "annotation_round": annotation_round,
                "created_at": created_at,
            }
        )
        records.append(record)
    return {
        "schema_version": "1.0",
        "scenario_id": scenario["scenario_id"],
        "annotation_module": module,
        "annotator_id": annotator_id,
        "annotation_round": annotation_round,
        "manual_version": manifest["manual_version"],
        "scenario_validity": payload.get("scenario_validity"),
        "records": records,
    }


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(dict(value), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(path)


def save_human_draft(
    *,
    payload: Mapping[str, Any],
    scenario: Mapping[str, Any],
    manifest: Mapping[str, Any],
    module: str,
    output_path: str | Path,
    flagged_for_review: bool,
    mark_complete: bool,
    updated_at: str | None = None,
    schema_dir: str | Path | None = None,
) -> dict[str, Any]:
    timestamp = updated_at or datetime.now(timezone.utc).isoformat()
    errors: list[str] = []
    annotation_document = None
    if mark_complete:
        completeness = check_annotation_completeness(payload, scenario, module)
        if not completeness.ok:
            errors.extend(completeness.messages())
        else:
            annotation_document = build_annotation_document(
                payload=payload,
                scenario=scenario,
                manifest=manifest,
                annotator_id="Human",
                annotation_round=str(manifest["annotation_round"]),
                module=module,
                created_at=timestamp,
            )
            validation_path = Path(output_path).with_suffix(".validation.json")
            _atomic_json(validation_path, annotation_document)
            try:
                artifact_type = {"A": "event-annotation", "B": "appraisal-annotation", "C": "bot-annotation"}[module]
                result = Validator(schema_dir).validate_paths(
                    [validation_path],
                    artifact_type,
                    scenarios={str(scenario["scenario_id"]): scenario},
                    require_scenario_context=True,
                )
            finally:
                validation_path.unlink(missing_ok=True)
            errors.extend(str(issue) for issue in result.issues)
            if errors:
                annotation_document = None
    draft = {
        "draft_version": "1.0",
        "status": "COMPLETE" if mark_complete and not errors else "IN_PROGRESS",
        "scenario_id": scenario["scenario_id"],
        "annotation_module": module,
        "annotator_id": "Human",
        "manual_version": manifest["manual_version"],
        "scenario_version": scenario["scenario_version"],
        "flagged_for_review": flagged_for_review,
        "updated_at": timestamp,
        "payload": {"scenario_validity": payload.get("scenario_validity"), "records": list(payload.get("records", []))},
        "validation_errors": errors,
        "annotation_document": annotation_document,
    }
    draft_schema = load_json(Path(schema_dir or Path(__file__).with_name("schemas")) / "human_draft.schema.json")
    schema_errors = list(Draft202012Validator(draft_schema, format_checker=FormatChecker()).iter_errors(draft))
    if schema_errors:
        raise ValueError("draft schema failure: " + "; ".join(error.message for error in schema_errors))
    _atomic_json(Path(output_path), draft)
    return draft


def export_annotations(
    *,
    annotator_id: str,
    module: str,
    assignment_path: str | Path,
    draft_dir: str | Path,
    output_path: str | Path,
) -> int:
    scenarios = {str(row["scenario_id"]): row for row in load_jsonl(assignment_path)}
    documents: list[dict[str, Any]] = []
    missing: list[str] = []
    for scenario_id in scenarios:
        path = Path(draft_dir) / f"{scenario_id}.json"
        if not path.exists():
            missing.append(scenario_id)
            continue
        value = load_json(path)
        if value.get("status") == "COMPLETE" and value.get("annotation_document"):
            document = value["annotation_document"]
        elif value.get("records") and value.get("annotation_module"):
            # AI imported drafts are already final annotation documents.
            document = value
        else:
            missing.append(scenario_id)
            continue
        if document.get("annotator_id") != annotator_id or document.get("annotation_module") != module:
            raise ValueError(f"{path}: annotator/module mismatch")
        completeness = check_annotation_completeness(document, scenarios[scenario_id], module)
        if not completeness.ok:
            raise ValueError(f"{path}: " + "; ".join(completeness.messages()))
        documents.append(document)
    if missing:
        raise ValueError(f"cannot export: missing or incomplete scenarios {missing}")
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    encoded = "".join(json.dumps(document, ensure_ascii=False, sort_keys=True) + "\n" for document in documents)
    temporary = output.with_name(output.name + ".validation.jsonl")
    temporary.write_text(encoded, encoding="utf-8", newline="\n")
    artifact_type = {"A": "event-annotation", "B": "appraisal-annotation", "C": "bot-annotation"}[module]
    try:
        result = Validator().validate_paths(
            [temporary], artifact_type, scenarios=scenarios, require_scenario_context=True
        )
        if not result.ok:
            raise ValueError(
                "export validation failed: "
                + "; ".join(str(issue) for issue in result.issues)
            )
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    return len(documents)
