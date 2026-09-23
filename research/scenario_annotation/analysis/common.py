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


def assignment_coverage_by_scenario(
    assignments_root: str | Path,
) -> dict[str, dict[str, set[str]]]:
    """Return assigned annotators by scenario and annotation module."""
    root = Path(assignments_root)
    coverage: dict[str, dict[str, set[str]]] = {}
    if not root.exists():
        return coverage
    for directory in sorted(path for path in root.iterdir() if path.is_dir()):
        manifest_path = directory / "manifest.json"
        if not manifest_path.is_file():
            continue
        manifest = load_json(manifest_path)
        annotator = str(manifest.get("annotator_id", ""))
        if not annotator:
            continue
        for entry in manifest.get("files", []):
            module = str(entry.get("module", ""))
            assignment_path = directory / str(entry.get("path", ""))
            if module not in {"A", "B", "C"} or not assignment_path.is_file():
                continue
            for row in load_jsonl(assignment_path):
                scenario_id = str(row.get("scenario_id", ""))
                if scenario_id:
                    coverage.setdefault(scenario_id, {}).setdefault(module, set()).add(annotator)
    return coverage


def load_annotation_documents(
    path: str | Path,
    *,
    validate: bool = True,
    scenarios: Mapping[str, Mapping[str, Any]] | None = None,
    require_scenario_context: bool = False,
) -> list[dict[str, Any]]:
    files = annotation_files(path)
    documents: list[dict[str, Any]] = []
    validator = Validator()
    for file_path in files:
        candidates = load_jsonl(file_path) if file_path.suffix == ".jsonl" else [load_json(file_path)]
        candidates = [
            candidate
            for candidate in candidates
            if isinstance(candidate, dict) and "annotation_module" in candidate
        ]
        modules = {str(candidate.get("annotation_module")) for candidate in candidates}
        if len(modules) > 1:
            raise ValueError(f"{file_path}: annotation file mixes modules {sorted(modules)}")
        if validate and candidates:
            artifact_type = {
                "A": "event-annotation",
                "B": "appraisal-annotation",
                "C": "bot-annotation",
            }.get(next(iter(modules)))
            if artifact_type is None:
                raise ValueError(f"{file_path}: unknown annotation module")
            result = validator.validate_paths(
                [file_path],
                artifact_type,
                scenarios=scenarios,
                require_scenario_context=require_scenario_context,
            )
            if not result.ok:
                detail = "\n".join(str(issue) for issue in result.issues)
                raise ValueError(f"annotation validation failed before analysis:\n{detail}")
        documents.extend(candidates)
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
