"""Canonical completeness and submission-integrity contracts for Stage 1."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from .annotation_catalog import FieldRequirement, field_specs
from .assignments import verify_assignment_manifest
from .loader import load_json, load_jsonl


AnnotationKey = tuple[str, str]
SubmissionKey = tuple[str, str, str]


def module_targets(scenario: Mapping[str, Any], module: str) -> list[str]:
    if module == "C":
        return [str(item["response_unit_ref"]) for item in scenario.get("bot_response_units", [])]
    refs: list[str] = []
    for collection in ("focal_events", "current_tasks"):
        for item in scenario.get(collection, []):
            ref = str(item.get("event_ref", ""))
            if ref and ref not in refs:
                refs.append(ref)
    return refs


def _conditional_keys(scenario: Mapping[str, Any], module: str) -> set[AnnotationKey]:
    keys: set[AnnotationKey] = set()
    for target in scenario.get("annotation_targets", []):
        if not isinstance(target, Mapping) or target.get("module") != module:
            continue
        target_ref = str(target.get("target_ref", ""))
        for variable in target.get("variables", []):
            if target_ref and isinstance(variable, str):
                keys.add((target_ref, variable))
    return keys


def expected_annotation_keys(scenario: Mapping[str, Any], module: str) -> set[AnnotationKey]:
    targets = module_targets(scenario, module)
    explicit = _conditional_keys(scenario, module)
    expected: set[AnnotationKey] = set()
    for spec in field_specs(module):
        if spec.requirement is FieldRequirement.REQUIRED:
            expected.update((target, spec.variable) for target in targets)
        elif spec.requirement is FieldRequirement.SCENARIO_CONDITIONAL:
            expected.update(key for key in explicit if key[1] == spec.variable)
    return expected


@dataclass(frozen=True)
class CompletenessResult:
    missing_keys: tuple[AnnotationKey, ...]
    unexpected_keys: tuple[AnnotationKey, ...]
    duplicate_keys: tuple[AnnotationKey, ...]

    @property
    def ok(self) -> bool:
        return not (self.missing_keys or self.unexpected_keys or self.duplicate_keys)

    def messages(self) -> list[str]:
        messages: list[str] = []
        if self.missing_keys:
            messages.append(f"missing required target/variable records: {list(self.missing_keys)}")
        if self.unexpected_keys:
            messages.append(f"unexpected target/variable records: {list(self.unexpected_keys)}")
        if self.duplicate_keys:
            messages.append(f"duplicate target/variable records: {list(self.duplicate_keys)}")
        return messages


def check_annotation_completeness(
    document: Mapping[str, Any], scenario: Mapping[str, Any], module: str
) -> CompletenessResult:
    expected = expected_annotation_keys(scenario, module)
    keys = [
        (str(record.get("target_ref", "")), str(record.get("variable", "")))
        for record in document.get("records", [])
        if record.get("label") not in (None, "")
    ]
    counts = Counter(keys)
    actual = set(keys)
    return CompletenessResult(
        missing_keys=tuple(sorted(expected - actual)),
        unexpected_keys=tuple(sorted(actual - expected)),
        duplicate_keys=tuple(sorted(key for key, count in counts.items() if count > 1)),
    )


@dataclass(frozen=True)
class SubmissionIntegrity:
    expected_documents: frozenset[SubmissionKey]
    actual_documents: frozenset[SubmissionKey]
    missing_documents: tuple[SubmissionKey, ...]
    unexpected_documents: tuple[SubmissionKey, ...]
    duplicate_documents: tuple[SubmissionKey, ...]
    incomplete_documents: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not (
            self.missing_documents
            or self.unexpected_documents
            or self.duplicate_documents
            or self.incomplete_documents
        )

    def detail(self) -> str:
        return (
            f"expected={len(self.expected_documents)}; actual={len(self.actual_documents)}; "
            f"missing={list(self.missing_documents)}; unexpected={list(self.unexpected_documents)}; "
            f"duplicates={list(self.duplicate_documents)}; incomplete={list(self.incomplete_documents)}"
        )


def expected_submission_documents(
    assignments_root: str | Path,
    *,
    annotators: Iterable[str] | None = None,
) -> tuple[
    set[SubmissionKey],
    dict[tuple[str, str], Mapping[str, Any]],
    dict[str, Mapping[str, Any]],
]:
    root = Path(assignments_root)
    selected = set(annotators) if annotators is not None else None
    expected: set[SubmissionKey] = set()
    scenarios: dict[tuple[str, str], Mapping[str, Any]] = {}
    provenance: dict[str, Mapping[str, Any]] = {}
    for manifest_path in sorted(root.glob("*/manifest.json")):
        unverified_manifest = load_json(manifest_path)
        manual_path = root.parent.parent / "manuals" / f"coding_manual_v{unverified_manifest['manual_version']}.md"
        manifest = verify_assignment_manifest(
            manifest_path,
            manual_path=manual_path,
        )
        annotator = str(manifest["annotator_id"])
        provenance[annotator] = manifest
        if selected is not None and annotator not in selected:
            continue
        for item in manifest["files"]:
            module = str(item["module"])
            for scenario in load_jsonl(manifest_path.parent / str(item["path"])):
                scenario_id = str(scenario["scenario_id"])
                expected.add((annotator, module, scenario_id))
                scenarios[(module, scenario_id)] = scenario
    if not expected:
        raise ValueError(f"no assignment documents found in {root}")
    return expected, scenarios, provenance


def check_submission_integrity(
    assignments_root: str | Path,
    documents: Iterable[Mapping[str, Any]],
    *,
    annotators: Iterable[str] | None = None,
) -> SubmissionIntegrity:
    expected, scenarios, provenance = expected_submission_documents(assignments_root, annotators=annotators)
    documents = list(documents)
    document_keys = [
        (
            str(document.get("annotator_id", "")),
            str(document.get("annotation_module", "")),
            str(document.get("scenario_id", "")),
        )
        for document in documents
    ]
    counts = Counter(document_keys)
    actual = set(document_keys)
    incomplete: list[str] = []
    for key, document in zip(document_keys, documents):
        manifest = provenance.get(key[0])
        if manifest is not None and (
            str(document.get("manual_version")) != str(manifest["manual_version"])
            or document.get("manual_sha256") != manifest["manual_sha256"]
        ):
            incomplete.append(f"{':'.join(key)}: annotation manual provenance does not match assignment manifest")
        scenario = scenarios.get((key[1], key[2]))
        if scenario is None:
            continue
        result = check_annotation_completeness(document, scenario, key[1])
        incomplete.extend(f"{':'.join(key)}: {message}" for message in result.messages())
    return SubmissionIntegrity(
        expected_documents=frozenset(expected),
        actual_documents=frozenset(actual),
        missing_documents=tuple(sorted(expected - actual)),
        unexpected_documents=tuple(sorted(actual - expected)),
        duplicate_documents=tuple(sorted(key for key, count in counts.items() if count > 1)),
        incomplete_documents=tuple(incomplete),
    )
