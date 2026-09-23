"""Regenerate Stage 1 calibration artifacts and compare committed bytes."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from .assignments import ANNOTATORS, build_assignments
from .artifact_fingerprint import sha256_file
from .corpus import write_calibration


CORPUS_ARTIFACTS = (
    Path("scenarios/calibration.jsonl"),
    Path("hidden/coverage_tags.jsonl"),
    Path("hidden/pair_design.jsonl"),
    Path("hidden/anchor_reference.jsonl"),
)

ASSIGNMENT_ARTIFACTS = tuple(
    Path("assignments/round_calibration")
    / annotator.lower().replace("-", "_")
    / filename
    for annotator in ANNOTATORS
    for filename in ("module_a.jsonl", "module_b.jsonl", "module_c.jsonl", "manifest.json")
)


def regenerate_calibration(output_root: str | Path) -> None:
    root = Path(output_root)
    write_calibration(root)
    build_assignments(
        root / "scenarios" / "calibration.jsonl",
        root / "hidden" / "pair_design.jsonl",
        root / "assignments" / "round_calibration",
        annotation_round="CALIBRATION",
        manual_version="0.1",
        scenario_version="0.1",
        seed=12001,
    )


def byte_mismatches(
    committed_root: str | Path,
    generated_root: str | Path,
    relative_paths: Iterable[Path],
) -> list[str]:
    committed = Path(committed_root)
    generated = Path(generated_root)
    mismatches: list[str] = []
    for relative_path in relative_paths:
        committed_path = committed / relative_path
        generated_path = generated / relative_path
        if not committed_path.exists():
            mismatches.append(f"missing committed artifact: {relative_path.as_posix()}")
        elif not generated_path.exists():
            mismatches.append(f"generator omitted artifact: {relative_path.as_posix()}")
        elif sha256_file(committed_path) != sha256_file(generated_path):
            mismatches.append(f"byte mismatch: {relative_path.as_posix()}")
    return mismatches
