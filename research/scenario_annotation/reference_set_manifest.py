"""Bind a Reference Set to its annotation, assignment, manual, and analysis inputs."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from .analysis.common import annotation_files
from .artifact_fingerprint import sha256_file, sha256_fileset
from .loader import load_json


def _artifact_files(root: str | Path) -> list[Path]:
    path = Path(root)
    if path.is_file():
        return [path]
    return sorted(
        item for item in path.rglob("*")
        if item.is_file() and item.suffix in {".json", ".jsonl"}
    )


def build_reference_set_manifest(
    *,
    reference_set_path: str | Path,
    analysis_manifest_path: str | Path,
    annotations_dir: str | Path,
    assignments_root: str | Path,
    manual_path: str | Path,
    scenario_paths: Iterable[str | Path],
    repository_root: str | Path,
) -> dict[str, Any]:
    reference = Path(reference_set_path)
    analysis_path = Path(analysis_manifest_path)
    annotation_paths = annotation_files(annotations_dir)
    assignment_paths = _artifact_files(assignments_root)
    scenarios = sorted((Path(path) for path in scenario_paths), key=lambda item: item.as_posix())
    if not reference.is_file() or not analysis_path.is_file():
        raise ValueError("reference set and source analysis manifest must exist")
    if not annotation_paths or not assignment_paths or not scenarios:
        raise ValueError("reference provenance requires annotation, assignment, and scenario files")

    annotation_hash = sha256_fileset(annotation_paths, repository_root=repository_root)
    scenario_hash = sha256_fileset(scenarios, repository_root=repository_root)
    manual_hash = sha256_file(manual_path)
    assignment_hash = sha256_fileset(assignment_paths, repository_root=repository_root)
    source_analysis = load_json(analysis_path)
    if source_analysis.get("annotation_fileset_sha256") != annotation_hash:
        raise ValueError("source analysis manifest does not match current annotation files")
    if source_analysis.get("scenario_fileset_sha256") != scenario_hash:
        raise ValueError("source analysis manifest does not match current scenario files")
    if source_analysis.get("manual_sha256") != manual_hash:
        raise ValueError("source analysis manifest does not match the current coding manual")
    if source_analysis.get("assignment_fileset_sha256") != assignment_hash:
        raise ValueError("source analysis manifest does not match current assignment files")

    return {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_analysis_manifest_sha256": sha256_file(analysis_path),
        "annotation_fileset_sha256": annotation_hash,
        "assignment_fileset_sha256": assignment_hash,
        "manual_sha256": manual_hash,
        "scenario_fileset_sha256": scenario_hash,
        "reference_set_sha256": sha256_file(reference),
    }


def write_reference_set_manifest(
    path: str | Path,
    manifest: Mapping[str, Any],
) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(dict(manifest), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
