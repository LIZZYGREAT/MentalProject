"""Create and verify analysis input snapshot manifests."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
from typing import Any, Iterable, Mapping

from .analysis.common import annotation_files
from .artifact_fingerprint import fingerprint, sha256_fileset, verify_fingerprint
from .loader import load_json


ANALYSIS_VERSION = "1.0"


def _git_revision(repository_root: Path) -> str:
    completed = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repository_root, check=True, capture_output=True, text=True)
    return completed.stdout.strip()


def build_analysis_manifest(
    *, annotation_paths: Iterable[str | Path], scenario_paths: Iterable[str | Path],
    coverage_path: str | Path, pair_design_path: str | Path,
    quality_thresholds_path: str | Path, manual_version: str,
    repository_root: str | Path,
) -> dict[str, Any]:
    annotations = sorted((Path(path).resolve() for path in annotation_paths), key=lambda item: item.as_posix())
    scenarios = sorted((Path(path).resolve() for path in scenario_paths), key=lambda item: item.as_posix())
    settings = load_json(quality_thresholds_path)
    return {
        "analysis_version": ANALYSIS_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "manual_version": manual_version,
        "scenario_files": [fingerprint(path) for path in scenarios],
        "scenario_fileset_sha256": sha256_fileset(scenarios),
        "coverage_file": fingerprint(coverage_path),
        "pair_design_file": fingerprint(pair_design_path),
        "annotation_artifacts": [fingerprint(path) for path in annotations],
        "annotation_fileset_sha256": sha256_fileset(annotations),
        "quality_threshold_version": settings["settings_version"],
        "quality_threshold_file": fingerprint(quality_thresholds_path),
        "git_revision": _git_revision(Path(repository_root)),
    }


def write_analysis_manifest(path: str | Path, manifest: Mapping[str, Any]) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(dict(manifest), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")


def verify_analysis_manifest_current(
    manifest_path: str | Path, *, annotations_dir: str | Path,
    scenario_paths: Iterable[str | Path], coverage_path: str | Path,
    pair_design_path: str | Path, quality_thresholds_path: str | Path,
) -> Mapping[str, Any]:
    manifest = load_json(manifest_path)
    entries = [*manifest.get("scenario_files", []), manifest.get("coverage_file", {}), manifest.get("pair_design_file", {}), *manifest.get("annotation_artifacts", []), manifest.get("quality_threshold_file", {})]
    errors = [error for entry in entries if (error := verify_fingerprint(entry))]
    if sha256_fileset(annotation_files(annotations_dir)) != manifest.get("annotation_fileset_sha256"):
        errors.append("annotation artifact set changed")
    if sha256_fileset(Path(path).resolve() for path in scenario_paths) != manifest.get("scenario_fileset_sha256"):
        errors.append("scenario artifact set changed")
    expected_paths = {Path(coverage_path).resolve().as_posix(), Path(pair_design_path).resolve().as_posix(), Path(quality_thresholds_path).resolve().as_posix()}
    recorded_paths = {str(manifest.get("coverage_file", {}).get("path")), str(manifest.get("pair_design_file", {}).get("path")), str(manifest.get("quality_threshold_file", {}).get("path"))}
    if expected_paths != recorded_paths:
        errors.append("analysis manifest input paths do not match the current session")
    if errors:
        raise ValueError("stale analysis manifest: " + "; ".join(errors))
    return manifest
