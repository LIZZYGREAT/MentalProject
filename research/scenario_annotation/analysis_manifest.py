"""Create and verify analysis input and implementation snapshots."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
from typing import Any, Iterable, Mapping

from .analysis.common import annotation_files
from .artifact_fingerprint import (
    fingerprint,
    logical_path,
    sha256_fileset,
    verify_fingerprint,
)
from .loader import load_json


ANALYSIS_VERSION = "1.1"
_PACKAGE_RELATIVE = Path("research") / "scenario_annotation"
_ANALYSIS_SUPPORT_FILES = (
    "analysis_manifest.py",
    "artifact_fingerprint.py",
    "annotation_catalog.py",
    "annotation_contract.py",
    "assignments.py",
    "validation.py",
)


def _package_root(repository_root: Path) -> Path:
    return repository_root / _PACKAGE_RELATIVE


def _analysis_code_paths(repository_root: Path) -> list[Path]:
    package_root = _package_root(repository_root)
    paths = list((package_root / "analysis").rglob("*.py"))
    paths.extend(package_root / name for name in _ANALYSIS_SUPPORT_FILES)
    paths.extend((package_root / "schemas").glob("*.schema.json"))
    paths = sorted((path.resolve() for path in paths), key=lambda path: path.as_posix())
    missing = [path for path in paths if not path.is_file()]
    if missing:
        raise ValueError("analysis implementation files are missing: " + ", ".join(map(str, missing)))
    if not paths:
        raise ValueError(f"no analysis implementation files found under {package_root}")
    return paths


def _git_revision(repository_root: Path) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def build_analysis_manifest(
    *,
    annotation_paths: Iterable[str | Path],
    scenario_paths: Iterable[str | Path],
    coverage_path: str | Path,
    pair_design_path: str | Path,
    quality_thresholds_path: str | Path,
    manual_version: str,
    repository_root: str | Path,
    anchor_reference_path: str | Path | None = None,
    anchor_review_decisions_path: str | Path | None = None,
    manual_path: str | Path | None = None,
    assignments_root: str | Path | None = None,
) -> dict[str, Any]:
    root = Path(repository_root).resolve()
    annotations = sorted((Path(path).resolve() for path in annotation_paths), key=lambda path: path.as_posix())
    scenarios = sorted((Path(path).resolve() for path in scenario_paths), key=lambda path: path.as_posix())
    settings = load_json(quality_thresholds_path)
    package_root = _package_root(root)
    anchor_reference = Path(anchor_reference_path or Path(coverage_path).parent / "anchor_reference.jsonl").resolve()
    review_decisions = Path(
        anchor_review_decisions_path
        or package_root / "adjudication" / "anchor_review_decisions.jsonl"
    ).resolve()
    manual = Path(
        manual_path or package_root / "manuals" / f"coding_manual_v{manual_version}.md"
    ).resolve()
    assignments = annotation_files(assignments_root) if assignments_root is not None else []
    code_paths = _analysis_code_paths(root)
    manual_fingerprint = fingerprint(manual, repository_root=root)
    return {
        "analysis_version": ANALYSIS_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "manual_version": manual_version,
        "manual_file": manual_fingerprint,
        "manual_sha256": manual_fingerprint["sha256"],
        "scenario_files": [fingerprint(path, repository_root=root) for path in scenarios],
        "scenario_fileset_sha256": sha256_fileset(scenarios, repository_root=root),
        "coverage_file": fingerprint(coverage_path, repository_root=root),
        "pair_design_file": fingerprint(pair_design_path, repository_root=root),
        "anchor_reference_file": fingerprint(anchor_reference, repository_root=root),
        "anchor_review_decisions_file": (
            fingerprint(review_decisions, repository_root=root)
            if review_decisions.is_file()
            else None
        ),
        "assignment_artifacts": [fingerprint(path, repository_root=root) for path in assignments],
        "assignment_fileset_sha256": (
            sha256_fileset(assignments, repository_root=root) if assignments else None
        ),
        "annotation_artifacts": [fingerprint(path, repository_root=root) for path in annotations],
        "annotation_fileset_sha256": sha256_fileset(annotations, repository_root=root),
        "quality_threshold_version": settings["settings_version"],
        "quality_threshold_file": fingerprint(quality_thresholds_path, repository_root=root),
        "analysis_code_files": [fingerprint(path, repository_root=root) for path in code_paths],
        "analysis_code_sha256": sha256_fileset(code_paths, repository_root=root),
        "git_revision": _git_revision(root),
    }


def write_analysis_manifest(path: str | Path, manifest: Mapping[str, Any]) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(dict(manifest), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def verify_analysis_manifest_current(
    manifest_path: str | Path,
    *,
    annotations_dir: str | Path,
    scenario_paths: Iterable[str | Path],
    coverage_path: str | Path,
    pair_design_path: str | Path,
    quality_thresholds_path: str | Path,
    anchor_reference_path: str | Path | None = None,
    anchor_review_decisions_path: str | Path | None = None,
    manual_path: str | Path | None = None,
    assignments_root: str | Path | None = None,
    repository_root: str | Path | None = None,
) -> Mapping[str, Any]:
    root = Path(repository_root or Path(__file__).resolve().parents[2]).resolve()
    package_root = _package_root(root)
    manifest = load_json(manifest_path)
    errors: list[str] = []
    if manifest.get("analysis_version") != ANALYSIS_VERSION:
        errors.append(
            "analysis version is stale: "
            f"manifest={manifest.get('analysis_version')!r}, current={ANALYSIS_VERSION!r}"
        )

    anchor_reference = Path(anchor_reference_path or Path(coverage_path).parent / "anchor_reference.jsonl").resolve()
    review_decisions = Path(
        anchor_review_decisions_path
        or package_root / "adjudication" / "anchor_review_decisions.jsonl"
    ).resolve()
    recorded_manual = manifest.get("manual_file", {})
    manual = Path(
        manual_path or root / Path(str(recorded_manual.get("logical_path", "")))
    ).resolve()
    code_paths = _analysis_code_paths(root)

    entries = [
        *manifest.get("scenario_files", []),
        manifest.get("coverage_file", {}),
        manifest.get("pair_design_file", {}),
        manifest.get("anchor_reference_file", {}),
        *([recorded_manual] if recorded_manual else []),
        *manifest.get("assignment_artifacts", []),
        *manifest.get("annotation_artifacts", []),
        manifest.get("quality_threshold_file", {}),
        *manifest.get("analysis_code_files", []),
    ]
    recorded_review = manifest.get("anchor_review_decisions_file")
    if recorded_review is not None:
        entries.append(recorded_review)
    errors.extend(
        error
        for entry in entries
        if (error := verify_fingerprint(entry, repository_root=root))
    )

    if sha256_fileset(annotation_files(annotations_dir), repository_root=root) != manifest.get(
        "annotation_fileset_sha256"
    ):
        errors.append("annotation artifact set changed")
    current_scenarios = [Path(path).resolve() for path in scenario_paths]
    if sha256_fileset(current_scenarios, repository_root=root) != manifest.get(
        "scenario_fileset_sha256"
    ):
        errors.append("scenario artifact set changed")
    if not recorded_manual or logical_path(manual, root) != recorded_manual.get("logical_path"):
        errors.append("analysis manifest manual path does not match the current session")

    if assignments_root is None:
        if manifest.get("assignment_artifacts"):
            errors.append("assignment root is required to verify the recorded assignment fileset")
    else:
        assignment_paths = annotation_files(assignments_root)
        actual_assignment_hash = (
            sha256_fileset(assignment_paths, repository_root=root) if assignment_paths else None
        )
        if actual_assignment_hash != manifest.get("assignment_fileset_sha256"):
            errors.append("assignment artifact set changed")
        recorded_assignment_paths = {
            str(entry.get("logical_path"))
            for entry in manifest.get("assignment_artifacts", [])
        }
        expected_assignment_paths = {logical_path(path, root) for path in assignment_paths}
        if expected_assignment_paths != recorded_assignment_paths:
            errors.append("analysis manifest assignment files do not match the current session")

    expected_paths = {
        logical_path(coverage_path, root),
        logical_path(pair_design_path, root),
        logical_path(quality_thresholds_path, root),
        logical_path(anchor_reference, root),
    }
    recorded_paths = {
        str(manifest.get("coverage_file", {}).get("logical_path")),
        str(manifest.get("pair_design_file", {}).get("logical_path")),
        str(manifest.get("quality_threshold_file", {}).get("logical_path")),
        str(manifest.get("anchor_reference_file", {}).get("logical_path")),
    }
    if expected_paths != recorded_paths:
        errors.append("analysis manifest input paths do not match the current session")
    if recorded_review is not None and recorded_review.get("logical_path") != logical_path(
        review_decisions, root
    ):
        errors.append("analysis manifest anchor review decision path does not match the current session")
    if (review_decisions.is_file()) != (recorded_review is not None):
        errors.append("anchor review decision artifact set changed")

    expected_code_paths = {logical_path(path, root) for path in code_paths}
    recorded_code_paths = {
        str(entry.get("logical_path")) for entry in manifest.get("analysis_code_files", [])
    }
    if expected_code_paths != recorded_code_paths:
        errors.append("analysis implementation file set changed")
    if sha256_fileset(code_paths, repository_root=root) != manifest.get("analysis_code_sha256"):
        errors.append("analysis implementation changed")

    if errors:
        raise ValueError("stale analysis manifest: " + "; ".join(errors))
    return manifest
