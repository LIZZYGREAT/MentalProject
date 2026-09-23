"""Manifest-driven local Stage 1 annotation and adjudication sessions."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .loader import load_json


def annotator_slug(annotator_id: str) -> str:
    return annotator_id.lower().replace("-", "_")


@dataclass(frozen=True)
class AnnotationSession:
    package_root: Path
    round_name: str
    annotator_id: str
    assignment_root: Path
    manifest: Mapping[str, Any]
    manual_version: str
    manual_path: Path
    scenario_version: str
    draft_root: Path

    @classmethod
    def resolve(
        cls, package_root: str | Path, *, round_name: str,
        annotator_id: str, draft_root: str | Path | None = None,
    ) -> "AnnotationSession":
        root = Path(package_root).resolve()
        assignment_root = root / "assignments" / f"round_{round_name}" / annotator_slug(annotator_id)
        manifest = load_json(assignment_root / "manifest.json")
        expected_round = round_name.upper()
        if manifest.get("annotator_id") != annotator_id:
            raise ValueError("assignment manifest annotator does not match the requested session")
        if manifest.get("annotation_round") != expected_round:
            raise ValueError("assignment manifest round does not match the requested session")
        manual_version = str(manifest["manual_version"])
        manual_path = root / "manuals" / f"coding_manual_v{manual_version}.md"
        if not manual_path.exists():
            raise ValueError(f"manual selected by assignment manifest does not exist: {manual_path}")
        return cls(
            package_root=root,
            round_name=round_name,
            annotator_id=annotator_id,
            assignment_root=assignment_root.resolve(),
            manifest=manifest,
            manual_version=manual_version,
            manual_path=manual_path.resolve(),
            scenario_version=str(manifest["scenario_version"]),
            draft_root=Path(
                draft_root
                or root / "annotations" / "drafts" / annotator_slug(annotator_id) / round_name
            ).resolve(),
        )


@dataclass(frozen=True)
class AdjudicationSession:
    package_root: Path
    round_name: str
    assignments_root: Path
    annotations_root: Path
    analysis_root: Path
    output_root: Path
    scenario_paths: tuple[Path, ...]
    coverage_path: Path
    pair_design_path: Path
    quality_thresholds_path: Path
    manual_version: str
    manual_path: Path

    @classmethod
    def resolve(cls, package_root: str | Path, *, round_name: str) -> "AdjudicationSession":
        root = Path(package_root).resolve()
        assignments_root = root / "assignments" / f"round_{round_name}"
        manifests = [load_json(path) for path in sorted(assignments_root.glob("*/manifest.json"))]
        if not manifests:
            raise ValueError(f"no assignment manifests found for round {round_name}")
        versions = {str(manifest["manual_version"]) for manifest in manifests}
        rounds = {str(manifest["annotation_round"]) for manifest in manifests}
        if len(versions) != 1 or rounds != {round_name.upper()}:
            raise ValueError("round assignment manifests do not share one manual/round configuration")
        manual_version = versions.pop()
        manual_path = root / "manuals" / f"coding_manual_v{manual_version}.md"
        if round_name == "calibration":
            scenario_paths = (root / "scenarios" / "calibration.jsonl",)
        else:
            scenario_paths = (root / "scenarios" / "main.jsonl", root / "scenarios" / "edge.jsonl")
        return cls(
            package_root=root,
            round_name=round_name,
            assignments_root=assignments_root,
            annotations_root=root / "annotations" / round_name,
            analysis_root=root / "analysis" / "outputs" / round_name,
            output_root=root / "adjudication" / "drafts" / round_name,
            scenario_paths=scenario_paths,
            coverage_path=root / "hidden" / "coverage_tags.jsonl",
            pair_design_path=root / "hidden" / "pair_design.jsonl",
            quality_thresholds_path=root / "settings" / "quality_thresholds_v1.json",
            manual_version=manual_version,
            manual_path=manual_path,
        )
