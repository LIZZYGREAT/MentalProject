"""Shared annotation loading and indexing helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from ..loader import load_json, load_jsonl
from ..validation import Validator


@dataclass(frozen=True)
class AnnotationRow:
    scenario_id: str
    module: str
    annotator_id: str
    target_ref: str
    variable: str
    label: Any
    annotation_id: str
    record: Mapping[str, Any]


def annotation_files(path: str | Path) -> list[Path]:
    root = Path(path)
    if root.is_file():
        return [root]
    return sorted(
        item
        for item in root.rglob("*")
        if item.is_file() and item.suffix in {".json", ".jsonl"}
    )


def load_annotation_documents(path: str | Path, *, validate: bool = True) -> list[dict[str, Any]]:
    files = annotation_files(path)
    documents: list[dict[str, Any]] = []
    validator = Validator()
    for file_path in files:
        candidates = load_jsonl(file_path) if file_path.suffix == ".jsonl" else [load_json(file_path)]
        for candidate in candidates:
            if not isinstance(candidate, dict) or "annotation_module" not in candidate:
                continue
            if validate:
                artifact_type = {
                    "A": "event-annotation",
                    "B": "appraisal-annotation",
                    "C": "bot-annotation",
                }.get(str(candidate.get("annotation_module")))
                if artifact_type is None:
                    raise ValueError(f"{file_path}: unknown annotation module")
                result = validator.validate_paths([file_path], artifact_type)
                if not result.ok:
                    detail = "\n".join(str(issue) for issue in result.issues)
                    raise ValueError(f"annotation validation failed before analysis:\n{detail}")
            documents.append(candidate)
    if not documents:
        raise ValueError(f"no annotation documents found in {path}")
    return documents


def flatten_annotations(documents: Iterable[Mapping[str, Any]]) -> list[AnnotationRow]:
    rows: list[AnnotationRow] = []
    for document in documents:
        for record in document.get("records", []):
            rows.append(
                AnnotationRow(
                    scenario_id=str(record.get("scenario_id", document.get("scenario_id", ""))),
                    module=str(record.get("annotation_module", document.get("annotation_module", ""))),
                    annotator_id=str(record.get("annotator_id", document.get("annotator_id", ""))),
                    target_ref=str(record.get("target_ref", "")),
                    variable=str(record.get("variable", "")),
                    label=record.get("label"),
                    annotation_id=str(record.get("annotation_id", "")),
                    record=record,
                )
            )
    return rows


def load_scenarios(path: str | Path) -> dict[str, dict[str, Any]]:
    return {str(row["scenario_id"]): row for row in load_jsonl(path)}


def load_hidden_by_scenario(path: str | Path) -> dict[str, set[str]]:
    return {
        str(row["scenario_id"]): {str(tag) for tag in row.get("coverage_tags", [])}
        for row in load_jsonl(path)
    }
