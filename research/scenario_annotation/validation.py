"""JSON Schema and semantic validation for Stage 1 artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from jsonschema import Draft202012Validator, FormatChecker

from .loader import ArtifactLoadError, iter_artifacts, load_json


FORBIDDEN_VISIBLE_KEYS = frozenset(
    {
        "coverage_tags",
        "pair_id",
        "pair_metadata",
        "manipulated_factor",
        "expected_labels",
        "expected_sensitive_constructs",
        "expected_invariant_constructs",
        "generator_rationale",
        "design_notes",
        "gold_label",
    }
)


@dataclass(frozen=True)
class ValidationIssue:
    source: str
    path: str
    message: str
    code: str

    def __str__(self) -> str:
        location = f"{self.source}{self.path}"
        return f"{location}: [{self.code}] {self.message}"


@dataclass(frozen=True)
class ValidationResult:
    checked: int
    issues: tuple[ValidationIssue, ...]

    @property
    def ok(self) -> bool:
        return not self.issues


class Validator:
    """Validates syntax first and then project-specific Stage 1 invariants."""

    def __init__(self, schema_dir: str | Path | None = None) -> None:
        self.schema_dir = Path(schema_dir or Path(__file__).with_name("schemas"))

    def _schema(self, artifact_type: str) -> Mapping[str, Any]:
        names = {
            "scenario": "scenario.schema.json",
            "event-annotation": "event_annotation.schema.json",
            "appraisal-annotation": "appraisal_annotation.schema.json",
            "bot-annotation": "bot_annotation.schema.json",
            "pair-design": "pair_design.schema.json",
            "adjudication": "adjudication.schema.json",
            "freeze-manifest": "freeze_manifest.schema.json",
        }
        try:
            name = names[artifact_type]
        except KeyError as exc:
            raise ValueError(f"unknown artifact type: {artifact_type}") from exc
        schema = load_json(self.schema_dir / name)
        if not isinstance(schema, Mapping):
            raise ValueError(f"schema {name} is not an object")
        return schema

    def validate_paths(self, paths: Iterable[str | Path], artifact_type: str) -> ValidationResult:
        schema = self._schema(artifact_type)
        schema_validator = Draft202012Validator(schema, format_checker=FormatChecker())
        issues: list[ValidationIssue] = []
        checked = 0
        try:
            artifacts = iter_artifacts(paths)
            for path, line_number, value in artifacts:
                checked += 1
                source = f"{path}:{line_number}"
                for error in sorted(schema_validator.iter_errors(value), key=lambda item: list(item.path)):
                    json_path = "".join(f"/{part}" for part in error.path)
                    issues.append(ValidationIssue(source, json_path, error.message, "SCHEMA"))
                if artifact_type == "scenario":
                    issues.extend(self._scenario_semantics(value, source))
                elif artifact_type.endswith("annotation"):
                    issues.extend(self._annotation_semantics(value, source))
        except ArtifactLoadError as exc:
            issues.append(ValidationIssue(str(next(iter(paths), "artifact")), "", str(exc), "LOAD"))
        return ValidationResult(checked=checked, issues=tuple(issues))

    def _scenario_semantics(self, scenario: Mapping[str, Any], source: str) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        for path, key in _walk_keys(scenario):
            if key in FORBIDDEN_VISIBLE_KEYS:
                issues.append(
                    ValidationIssue(source, path, f"hidden key {key!r} is annotator-visible", "HIDDEN_METADATA")
                )

        cutoff = _parse_time(scenario.get("known_at_cutoff"))
        if cutoff is not None:
            for path, value in _walk_objects(scenario):
                known_at = _parse_time(value.get("known_at"))
                if known_at is not None and known_at > cutoff:
                    issues.append(
                        ValidationIssue(
                            source,
                            f"{path}/known_at",
                            "visible information is later than known_at_cutoff",
                            "FUTURE_KNOWLEDGE",
                        )
                    )

        for path, event in _named_objects(scenario, ("current_tasks", "focal_events")):
            progress = event.get("progress")
            remaining = event.get("remaining_effort")
            if progress == 1 and remaining not in (None, 0, "0", "UNKNOWN"):
                issues.append(
                    ValidationIssue(source, path, "completed task has positive remaining effort", "TASK_FACT_CONFLICT")
                )
            actual_start = _parse_time(event.get("actual_start"))
            actual_end = _parse_time(event.get("actual_end"))
            fraction = event.get("exposure_fraction")
            if actual_start and actual_end and fraction not in (None, "UNKNOWN"):
                issues.append(
                    ValidationIssue(
                        source,
                        path,
                        "actual interval and exposure_fraction encode the same partial fact",
                        "EXPOSURE_DOUBLE_ENCODING",
                    )
                )
        return issues

    def _annotation_semantics(self, document: Mapping[str, Any], source: str) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        for index, record in enumerate(document.get("records", [])):
            if not isinstance(record, Mapping):
                continue
            label = record.get("label")
            if label in {"UNKNOWN", "NO_EVIDENCE", "AMBIGUOUS"} and not record.get("unknown_reason"):
                issues.append(
                    ValidationIssue(
                        source,
                        f"/records/{index}/unknown_reason",
                        f"{label} requires unknown_reason",
                        "MISSING_UNKNOWN_REASON",
                    )
                )
            if (
                document.get("annotation_module") == "B"
                and label not in {"NO_EVIDENCE", "AMBIGUOUS"}
                and not record.get("evidence_span")
            ):
                issues.append(
                    ValidationIssue(
                        source,
                        f"/records/{index}/evidence_span",
                        "directional appraisal evidence requires evidence_span",
                        "MISSING_EVIDENCE_SPAN",
                    )
                )
            if label == "PARTIAL" and not record.get("partial_encoding_basis"):
                issues.append(
                    ValidationIssue(
                        source,
                        f"/records/{index}/partial_encoding_basis",
                        "PARTIAL exposure requires partial_encoding_basis",
                        "MISSING_PARTIAL_BASIS",
                    )
                )
        return issues


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _walk_keys(value: Any, path: str = "") -> Iterable[tuple[str, str]]:
    if isinstance(value, Mapping):
        for key, child in value.items():
            child_path = f"{path}/{key}"
            yield child_path, str(key)
            yield from _walk_keys(child, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_keys(child, f"{path}/{index}")


def _walk_objects(value: Any, path: str = "") -> Iterable[tuple[str, Mapping[str, Any]]]:
    if isinstance(value, Mapping):
        yield path, value
        for key, child in value.items():
            yield from _walk_objects(child, f"{path}/{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_objects(child, f"{path}/{index}")


def _named_objects(value: Mapping[str, Any], names: tuple[str, ...]) -> Iterable[tuple[str, Mapping[str, Any]]]:
    for name in names:
        children = value.get(name, [])
        if isinstance(children, list):
            for index, child in enumerate(children):
                if isinstance(child, Mapping):
                    yield f"/{name}/{index}", child
