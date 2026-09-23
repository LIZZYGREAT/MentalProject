"""Build a complete Stage 1 Reference Set without majority-vote shortcuts."""

from __future__ import annotations

from collections import defaultdict
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from jsonschema import Draft202012Validator, FormatChecker

from .annotation_catalog import BY_VARIABLE
from .analysis.common import flatten_annotations
from .annotation_contract import (
    check_submission_integrity,
    expected_annotation_keys,
    expected_submission_documents,
)
from .loader import load_json, load_jsonl
from .validation import Validator


ReferenceKey = tuple[str, str, str]


def expected_reference_keys(
    scenarios: Iterable[Mapping[str, Any]],
) -> set[ReferenceKey]:
    keys: set[ReferenceKey] = set()
    for scenario in scenarios:
        scenario_id = str(scenario["scenario_id"])
        for module in scenario.get("annotation_modules", []):
            keys.update(
                (scenario_id, target_ref, variable)
                for target_ref, variable in expected_annotation_keys(scenario, str(module))
            )
    return keys


def _load_records(path: str | Path) -> list[dict[str, Any]]:
    root = Path(path)
    if root.is_file():
        return load_jsonl(root) if root.suffix == ".jsonl" else [load_json(root)]
    rows: list[dict[str, Any]] = []
    for item in sorted(root.rglob("*")):
        if item.suffix == ".jsonl":
            rows.extend(load_jsonl(item))
        elif item.suffix == ".json":
            rows.append(load_json(item))
    return rows


def build_reference_set(
    *,
    annotation_documents: Iterable[Mapping[str, Any]],
    scenarios: Iterable[Mapping[str, Any]],
    adjudications: Iterable[Mapping[str, Any]],
    assignments_root: str | Path,
) -> list[dict[str, Any]]:
    annotation_documents = list(annotation_documents)
    integrity = check_submission_integrity(assignments_root, annotation_documents)
    if not integrity.ok:
        raise ValueError("SUBMISSION_INCOMPLETE: " + integrity.detail())
    expected_documents, _ = expected_submission_documents(assignments_root)
    scenario_index = {str(scenario["scenario_id"]): scenario for scenario in scenarios}
    expected = expected_reference_keys(scenario_index.values())
    grouped: dict[ReferenceKey, list[Any]] = defaultdict(list)
    for row in flatten_annotations(annotation_documents):
        grouped[(row.scenario_id, row.target_ref, row.variable)].append(row)
    adjudication_index = {
        (str(row["scenario_id"]), str(row["target_ref"]), str(row["variable"])): row
        for row in adjudications
    }
    missing_annotations = sorted(expected - set(grouped))
    unexpected_annotations = sorted(set(grouped) - expected)
    if missing_annotations or unexpected_annotations:
        raise ValueError(
            f"reference input key mismatch; missing={missing_annotations}; unexpected={unexpected_annotations}"
        )

    records: list[dict[str, Any]] = []
    unresolved: list[ReferenceKey] = []
    for key in sorted(expected):
        rows = grouped[key]
        spec = BY_VARIABLE.get(key[2])
        if spec is None:
            raise ValueError(f"reference key uses unknown annotation variable {key[2]!r}")
        expected_annotators = {
            annotator
            for annotator, module, scenario_id in expected_documents
            if module == spec.module and scenario_id == key[0]
        }
        source_annotators = {row.annotator_id for row in rows}
        if source_annotators != expected_annotators:
            raise ValueError(
                f"assigned annotator mismatch for {key}: "
                f"source={sorted(source_annotators)}; expected={sorted(expected_annotators)}"
            )
        if len(rows) != len(source_annotators):
            raise ValueError(f"duplicate annotation source for reference key {key}")
        labels = {json.dumps(row.label, ensure_ascii=False, sort_keys=True) for row in rows}
        source_ids = sorted(row.annotation_id for row in rows)
        manual_versions = {str(row.record.get("manual_version")) for row in rows}
        scenario_versions = {str(row.record.get("scenario_version")) for row in rows}
        if len(manual_versions) != 1 or len(scenario_versions) != 1:
            raise ValueError(f"source annotations for {key} do not share manual/scenario versions")
        if len(labels) == 1:
            resolution_mode = "UNANIMOUS"
            gold_label = rows[0].label
        else:
            adjudication = adjudication_index.get(key)
            if adjudication is None:
                unresolved.append(key)
                continue
            resolution_mode = "ADJUDICATED"
            gold_label = adjudication["gold_label"]
            adjudicated_sources = set(str(value) for value in adjudication["source_annotation_ids"])
            if adjudicated_sources != set(source_ids):
                raise ValueError(f"adjudication source annotations do not match current inputs for {key}")
        records.append(
            {
                "reference_id": f"REFERENCE:{key[0]}:{key[1]}:{key[2]}",
                "scenario_id": key[0],
                "target_ref": key[1],
                "variable": key[2],
                "gold_label": gold_label,
                "resolution_mode": resolution_mode,
                "source_annotation_ids": source_ids,
                "source_annotator_count": len(source_annotators),
                "expected_annotator_count": len(expected_annotators),
                "manual_version": manual_versions.pop(),
                "scenario_version": scenario_versions.pop(),
            }
        )
    if unresolved:
        raise ValueError(f"ADJUDICATION_REQUIRED for disagreement keys: {unresolved}")
    return records


def write_reference_set(
    path: str | Path,
    records: Iterable[Mapping[str, Any]],
    *,
    schema_dir: str | Path | None = None,
) -> int:
    records = [dict(record) for record in records]
    schema = Validator(schema_dir)._schema("reference-record")
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = [
        f"row {index}: {error.message}"
        for index, record in enumerate(records, start=1)
        for error in validator.iter_errors(record)
    ]
    if errors:
        raise ValueError("reference set schema failure: " + "; ".join(errors))
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    temporary.write_text(
        "".join(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n" for record in records),
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(output)
    return len(records)


def load_adjudications(path: str | Path) -> list[dict[str, Any]]:
    return _load_records(path)
