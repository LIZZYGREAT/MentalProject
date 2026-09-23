import json
from pathlib import Path

from research.scenario_annotation.assignments import ANNOTATORS, build_assignments
from research.scenario_annotation.corpus import write_calibration
from research.scenario_annotation.loader import load_json, load_jsonl
from research.scenario_annotation.validation import FORBIDDEN_VISIBLE_KEYS, Validator


def _build(tmp_path: Path) -> tuple[Path, Path]:
    corpus_root = tmp_path / "corpus"
    assignments_root = tmp_path / "assignments"
    write_calibration(corpus_root)
    build_assignments(
        corpus_root / "scenarios" / "calibration.jsonl",
        corpus_root / "hidden" / "pair_design.jsonl",
        assignments_root,
        annotation_round="CALIBRATION",
        manual_version="0.1",
        manual_path=Path(__file__).parents[1] / "manuals" / "coding_manual_v0.1.md",
        scenario_version="0.1",
        seed=12001,
    )
    return corpus_root, assignments_root


def test_assignment_manifests_bind_versions_and_validate(tmp_path) -> None:
    _, root = _build(tmp_path)
    validator = Validator()
    for annotator in ANNOTATORS:
        manifest_path = root / annotator.lower().replace("-", "_") / "manifest.json"
        manifest = load_json(manifest_path)
        assert manifest["manual_version"] == "0.1"
        assert manifest["scenario_version"] == "0.1"
        result = validator.validate_paths([manifest_path], "assignment-manifest")
        assert result.ok, [str(issue) for issue in result.issues]


def test_no_annotator_sees_both_presentation_variants(tmp_path) -> None:
    corpus_root, root = _build(tmp_path)
    pairs = [
        pair
        for pair in load_jsonl(corpus_root / "hidden" / "pair_design.jsonl")
        if pair["manipulated_factor"] == "presentation_mode"
    ]
    for annotator in ANNOTATORS:
        folder = root / annotator.lower().replace("-", "_")
        assigned = {
            row["scenario_id"]
            for module in ("a", "b", "c")
            for row in load_jsonl(folder / f"module_{module}.jsonl")
        }
        for pair in pairs:
            assert len(assigned.intersection(pair["scenario_ids"])) == 1
    for pair in pairs:
        left, right = pair["scenario_ids"]
        left_count = right_count = 0
        for annotator in ANNOTATORS:
            assigned = {
                row["scenario_id"]
                for row in load_jsonl(
                    root / annotator.lower().replace("-", "_") / "module_a.jsonl"
                )
            }
            left_count += left in assigned
            right_count += right in assigned
        assert left_count == right_count == len(ANNOTATORS) // 2


def test_assignment_views_are_blind_and_purpose_scoped(tmp_path) -> None:
    _, root = _build(tmp_path)
    for annotator in ANNOTATORS:
        folder = root / annotator.lower().replace("-", "_")
        for module in ("a", "b", "c"):
            rows = load_jsonl(folder / f"module_{module}.jsonl")
            serialized = json.dumps(rows, ensure_ascii=False)
            for key in FORBIDDEN_VISIBLE_KEYS:
                assert f'"{key}"' not in serialized
            assert all(row["annotation_modules"] == [module.upper()] for row in rows)
            if module in {"a", "b"}:
                assert all(not row["bot_response_units"] for row in rows)
            if module in {"b", "c"}:
                assert all(not row["recurring_course_context"] for row in rows)


def test_assignment_order_is_deterministic_but_differs_by_annotator(tmp_path) -> None:
    _, root = _build(tmp_path)
    orders = []
    for annotator in ANNOTATORS:
        rows = load_jsonl(root / annotator.lower().replace("-", "_") / "module_a.jsonl")
        orders.append([row["scenario_id"] for row in rows])
    assert len({tuple(order) for order in orders}) == len(ANNOTATORS)
