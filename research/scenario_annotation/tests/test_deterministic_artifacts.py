from pathlib import Path

from research.scenario_annotation.determinism import (
    ASSIGNMENT_ARTIFACTS,
    CORPUS_ARTIFACTS,
    byte_mismatches,
    regenerate_calibration,
)


PACKAGE_ROOT = Path(__file__).parents[1]


def test_generated_corpus_matches_committed_artifacts(tmp_path: Path) -> None:
    regenerate_calibration(tmp_path)
    assert byte_mismatches(PACKAGE_ROOT, tmp_path, CORPUS_ARTIFACTS) == []


def test_generated_assignments_match_committed_artifacts(tmp_path: Path) -> None:
    regenerate_calibration(tmp_path)
    assert byte_mismatches(PACKAGE_ROOT, tmp_path, ASSIGNMENT_ARTIFACTS) == []
