"""Validate model-controlled fields, add system fields, and save an atomic draft."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from jsonschema import Draft202012Validator, FormatChecker

from ..annotation_contract import check_annotation_completeness
from ..loader import load_json, load_jsonl
from ..validation import Validator


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(dict(value), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(path)


def import_ai_output(
    *,
    annotator_id: str,
    annotation_round: str,
    module: str,
    scenario_id: str,
    assignment_path: str | Path,
    assignment_manifest_path: str | Path,
    raw_output_path: str | Path,
    raw_output_schema_path: str | Path,
    output_dir: str | Path,
    provider: str,
    model: str,
    temperature: float,
    seed: int | None,
    request_id: str,
    attempt: int,
    created_at: str | None = None,
) -> Path:
    if attempt < 1:
        raise ValueError("attempt must be at least 1")
    manifest = load_json(assignment_manifest_path)
    if manifest.get("annotator_id") != annotator_id or manifest.get("annotation_round") != annotation_round:
        raise ValueError("assignment manifest does not match annotator/round")
    scenarios = {str(row["scenario_id"]): row for row in load_jsonl(assignment_path)}
    scenario = scenarios.get(scenario_id)
    if scenario is None:
        raise ValueError(f"scenario {scenario_id} is not present in the annotator assignment")
    if scenario.get("annotation_modules") != [module]:
        raise ValueError(f"scenario {scenario_id} is not assigned for Module {module}")

    raw_bytes = Path(raw_output_path).read_bytes()
    try:
        raw = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"AI output is not valid UTF-8 JSON: {exc}") from exc
    schema = load_json(raw_output_schema_path)
    errors = sorted(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(raw),
        key=lambda item: list(item.path),
    )
    if errors:
        detail = "; ".join(error.message for error in errors)
        raise ValueError(f"AI output contract failure: {detail}")

    timestamp = created_at or datetime.now(timezone.utc).isoformat()
    records: list[dict[str, Any]] = []
    for raw_record in raw["records"]:
        variable = str(raw_record["variable"])
        target_ref = str(raw_record["target_ref"])
        record = dict(raw_record)
        record.update(
            {
                "annotation_id": f"{scenario_id}:{module}:{target_ref}:{variable}:{annotator_id}",
                "scenario_id": scenario_id,
                "scenario_version": scenario["scenario_version"],
                "annotation_module": module,
                "manual_version": manifest["manual_version"],
                "annotator_id": annotator_id,
                "annotation_round": annotation_round,
                "created_at": timestamp,
            }
        )
        records.append(record)
    document = {
        "schema_version": "1.0",
        "scenario_id": scenario_id,
        "annotation_module": module,
        "annotator_id": annotator_id,
        "annotation_round": annotation_round,
        "manual_version": manifest["manual_version"],
        "scenario_validity": raw["scenario_validity"],
        "runner_provenance": {
            "provider": provider,
            "model": model,
            "prompt_manual_version": manifest["manual_version"],
            "temperature": temperature,
            "seed": seed,
            "request_id": request_id,
            "attempt": attempt,
            "raw_output_sha256": hashlib.sha256(raw_bytes).hexdigest(),
        },
        "records": records,
    }
    completeness = check_annotation_completeness(document, scenario, module)
    if not completeness.ok:
        raise ValueError("formal annotation completeness failure: " + "; ".join(completeness.messages()))
    artifact_type = {"A": "event-annotation", "B": "appraisal-annotation", "C": "bot-annotation"}[module]
    draft_path = Path(output_dir) / f"{scenario_id}.json"
    # Validate the enriched document through a temporary sibling before publishing it.
    temporary = draft_path.with_suffix(".validation.json")
    _atomic_json(temporary, document)
    try:
        result = Validator().validate_paths(
            [temporary],
            artifact_type,
            scenarios={scenario_id: scenario},
            require_scenario_context=True,
        )
    finally:
        temporary.unlink(missing_ok=True)
    if not result.ok:
        raise ValueError("enriched annotation validation failed: " + "; ".join(str(issue) for issue in result.issues))
    _atomic_json(draft_path, document)
    return draft_path
