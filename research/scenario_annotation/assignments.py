"""Blind, counterbalanced assignment construction for Stage 1 annotation rounds."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import random
from typing import Any, Iterable, Mapping

from .artifact_fingerprint import sha256_file
from .loader import load_json, load_jsonl
from .validation import Validator


ANNOTATORS = ("AI-A", "AI-B", "AI-C", "Human")
MODULES = ("A", "B", "C")


def _slug(value: str) -> str:
    return value.lower().replace("-", "_")


def _seed(seed: int, *parts: str) -> int:
    material = ":".join((str(seed), *parts)).encode("utf-8")
    return int.from_bytes(hashlib.sha256(material).digest()[:8], "big")


def _source_refs(value: Any) -> set[str]:
    refs: set[str] = set()
    if isinstance(value, Mapping):
        source_ref = value.get("source_ref")
        if isinstance(source_ref, str):
            refs.add(source_ref)
        for child in value.values():
            refs.update(_source_refs(child))
    elif isinstance(value, list):
        for child in value:
            refs.update(_source_refs(child))
    return refs


def purpose_scoped_view(scenario: Mapping[str, Any], module: str) -> dict[str, Any]:
    """Return a schema-valid view containing only the requested module's context."""
    view = json.loads(json.dumps(scenario, ensure_ascii=False))
    view["annotation_modules"] = [module]
    if module == "A":
        view["bot_response_units"] = []
    elif module == "B":
        view["recurring_course_context"] = []
        view["bot_response_units"] = []
    elif module == "C":
        view["recurring_course_context"] = []
    else:
        raise ValueError(f"unknown annotation module: {module}")
    view["source_refs"] = sorted(_source_refs({key: value for key, value in view.items() if key != "source_refs"}))
    return view


def _counterbalanced_ids(
    scenarios: list[Mapping[str, Any]],
    pairs: list[Mapping[str, Any]],
    annotator_index: int,
) -> set[str]:
    selected = {str(scenario["scenario_id"]) for scenario in scenarios}
    presentation_pairs = [pair for pair in pairs if pair.get("comparison_mode") == "BETWEEN_GROUPS"]
    for pair_index, pair in enumerate(sorted(presentation_pairs, key=lambda item: str(item["pair_id"]))):
        left, right = (str(item) for item in pair["scenario_ids"])
        keep = (left, right)[(annotator_index + pair_index) % 2]
        selected.discard(right if keep == left else left)
    return selected


def _write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> tuple[int, str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = "".join(
        json.dumps(dict(row), ensure_ascii=False, sort_keys=True) + "\n" for row in rows
    ).encode("utf-8")
    path.write_bytes(encoded)
    return encoded.count(b"\n"), hashlib.sha256(encoded).hexdigest()


def build_assignments(
    scenario_path: str | Path,
    pair_design_path: str | Path,
    output_dir: str | Path,
    *,
    annotation_round: str,
    manual_version: str,
    manual_path: str | Path,
    scenario_version: str,
    seed: int,
) -> dict[str, Any]:
    manual_path = Path(manual_path)
    if not manual_path.is_file():
        raise ValueError(f"coding manual does not exist: {manual_path}")
    manual_sha256 = sha256_file(manual_path)
    scenarios = load_jsonl(scenario_path)
    pairs = load_jsonl(pair_design_path)
    scenario_index = {str(row["scenario_id"]): row for row in scenarios}
    pair_validation = Validator().validate_paths(
        [pair_design_path], "pair-design", scenarios=scenario_index
    )
    if not pair_validation.ok:
        raise ValueError(
            "pair design validation failed before assignment: "
            + "; ".join(str(issue) for issue in pair_validation.issues)
        )
    root = Path(output_dir)
    summary: dict[str, Any] = {"annotators": {}, "seed": seed}
    for annotator_index, annotator in enumerate(ANNOTATORS):
        selected_ids = _counterbalanced_ids(scenarios, pairs, annotator_index)
        annotator_dir = root / _slug(annotator)
        file_records: list[dict[str, Any]] = []
        for module in MODULES:
            rows = [
                purpose_scoped_view(scenario, module)
                for scenario in scenarios
                if str(scenario["scenario_id"]) in selected_ids
                and module in scenario["annotation_modules"]
            ]
            random.Random(_seed(seed, annotator, module)).shuffle(rows)
            path = annotator_dir / f"module_{module.lower()}.jsonl"
            count, digest = _write_jsonl(path, rows)
            file_records.append(
                {
                    "module": module,
                    "path": path.name,
                    "scenario_count": count,
                    "sha256": digest,
                }
            )
        manifest = {
            "assignment_version": "1.0",
            "annotation_round": annotation_round,
            "annotator_id": annotator,
            "manual_version": manual_version,
            "manual_sha256": manual_sha256,
            "scenario_version": scenario_version,
            "randomization_seed": seed,
            "counterbalance_rule": "one presentation variant per pair per annotator",
            "files": file_records,
        }
        manifest_path = annotator_dir / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        summary["annotators"][annotator] = {
            "selected_scenarios": len(selected_ids),
            "files": file_records,
        }
    return summary


def verify_assignment_manifest(
    manifest_path: str | Path,
    *,
    manual_path: str | Path,
    assignment_path: str | Path | None = None,
    annotator_id: str | None = None,
    annotation_round: str | None = None,
    module: str | None = None,
) -> dict[str, Any]:
    """Validate a manifest, every assigned file hash/count, and its coding manual."""
    manifest_path = Path(manifest_path)
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError(f"assignment manifest is missing or is a symlink: {manifest_path}")
    root = manifest_path.parent.resolve()
    result = Validator().validate_paths([manifest_path], "assignment-manifest")
    if not result.ok:
        raise ValueError("invalid assignment manifest: " + "; ".join(str(issue) for issue in result.issues))
    manifest = load_json(manifest_path)
    if annotator_id is not None and manifest["annotator_id"] != annotator_id:
        raise ValueError("assignment manifest annotator does not match the requested session")
    if annotation_round is not None and manifest["annotation_round"] != annotation_round:
        raise ValueError("assignment manifest round does not match the requested session")
    if sha256_file(manual_path) != manifest["manual_sha256"]:
        raise ValueError("coding manual content hash does not match the assignment manifest")

    files_by_module: dict[str, Path] = {}
    for entry in manifest["files"]:
        current_module = str(entry["module"])
        if current_module in files_by_module:
            raise ValueError(f"assignment manifest repeats Module {current_module}")
        path = root / str(entry["path"])
        if path.is_symlink() or path.resolve().parent != root or not path.is_file():
            raise ValueError(f"assignment file is missing or escapes its annotator directory: {path}")
        rows = load_jsonl(path)
        if len(rows) != entry["scenario_count"]:
            raise ValueError(f"assignment row count does not match manifest: {path}")
        if sha256_file(path) != entry["sha256"]:
            raise ValueError(f"assignment file hash does not match manifest: {path}")
        files_by_module[current_module] = path
    if set(files_by_module) != set(MODULES):
        raise ValueError(f"assignment manifest modules must be exactly {sorted(MODULES)}")
    if assignment_path is not None:
        if module not in files_by_module:
            raise ValueError("module is required and must exist when verifying an assignment file")
        if Path(assignment_path).resolve() != files_by_module[module].resolve():
            raise ValueError("requested assignment file does not match the manifest module path")
    return manifest
