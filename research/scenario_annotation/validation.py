"""JSON Schema and semantic validation for Stage 1 artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
import re
from typing import Any, Iterable, Mapping
import unicodedata

from jsonschema import Draft202012Validator, FormatChecker

from .annotation_catalog import EVENT_SUBTYPES, LIFECYCLES
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

COURSE_PERIOD_STARTS = {
    time(8, 0), time(8, 55), time(10, 0), time(10, 55), time(12, 0),
    time(12, 55), time(14, 0), time(14, 55), time(16, 0), time(16, 55),
    time(18, 30), time(19, 25), time(20, 30), time(21, 25),
}
COURSE_PERIOD_ENDS = {
    time(8, 45), time(9, 40), time(10, 45), time(11, 40), time(12, 45),
    time(13, 40), time(14, 45), time(15, 40), time(16, 45), time(17, 40),
    time(19, 15), time(20, 10), time(21, 15), time(22, 10),
}

DATETIME_FACTS = {"SCHEDULED_START", "SCHEDULED_END", "ACTUAL_START", "ACTUAL_END", "DEADLINE"}
EFFORT_FACTS = {"ESTIMATED_TOTAL_EFFORT", "REMAINING_EFFORT"}


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
            "anchor-reference": "anchor_reference.schema.json",
            "coverage-tags": "coverage_tags.schema.json",
            "assignment-manifest": "assignment_manifest.schema.json",
            "revision-log": "revision_log.schema.json",
            "gate-result": "gate_result.schema.json",
            "quality-thresholds": "quality_thresholds.schema.json",
            "ai-output": "ai_output.schema.json",
            "human-draft": "human_draft.schema.json",
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

    def validate_paths(
        self,
        paths: Iterable[str | Path],
        artifact_type: str,
        *,
        scenarios: Mapping[str, Mapping[str, Any]] | None = None,
        require_scenario_context: bool = False,
    ) -> ValidationResult:
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
                    scenario = scenarios.get(str(value.get("scenario_id"))) if scenarios else None
                    issues.extend(
                        self._annotation_semantics(
                            value,
                            source,
                            scenario=scenario,
                            require_scenario_context=require_scenario_context,
                        )
                    )
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
        issues.extend(self._course_semantics(scenario, source))
        issues.extend(self._event_overlap_semantics(scenario, source))
        return issues

    def _course_semantics(self, scenario: Mapping[str, Any], source: str) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        context_text = " ".join(
            str(item.get("text", ""))
            for item in scenario.get("recent_context", [])
            if isinstance(item, Mapping)
        )
        edge_explained = any(token in context_text for token in ("调课", "停课", "节假日", "临时课程变更"))
        for index, course in enumerate(scenario.get("recurring_course_context", [])):
            if not isinstance(course, Mapping):
                continue
            path = f"/recurring_course_context/{index}"
            start = _parse_clock(course.get("start_time"))
            end = _parse_clock(course.get("end_time"))
            if (start not in COURSE_PERIOD_STARTS or end not in COURSE_PERIOD_ENDS or (start and end and start >= end)) and not edge_explained:
                issues.append(
                    ValidationIssue(
                        source,
                        path,
                        "course time is outside the standard timetable without an explicit schedule-change context",
                        "COURSE_TIMETABLE",
                    )
                )
            if any(day in {6, 7} for day in course.get("weekdays", [])) and not edge_explained:
                issues.append(
                    ValidationIssue(
                        source,
                        f"{path}/weekdays",
                        "weekend recurring course requires an explicit edge-case explanation",
                        "WEEKEND_RECURRING_COURSE",
                    )
                )
            if course.get("week2_same_as_week1") is False and not edge_explained:
                issues.append(
                    ValidationIssue(
                        source,
                        f"{path}/week2_same_as_week1",
                        "Week 2 differs from Week 1 without an explicit schedule-change context",
                        "TWO_WEEK_COURSE_INCONSISTENCY",
                    )
                )
        return issues

    def _event_overlap_semantics(self, scenario: Mapping[str, Any], source: str) -> list[ValidationIssue]:
        unique_events: dict[str, Mapping[str, Any]] = {}
        for _, event in _named_objects(scenario, ("current_tasks", "focal_events")):
            event_ref = str(event.get("event_ref", ""))
            if event_ref:
                unique_events[event_ref] = event
        intervals: list[tuple[str, datetime, datetime]] = []
        for event_ref, event in unique_events.items():
            start = _parse_time(event.get("actual_start") or event.get("scheduled_start"))
            end = _parse_time(event.get("actual_end") or event.get("scheduled_end"))
            if start is not None and end is not None:
                intervals.append((event_ref, start, end))
        issues: list[ValidationIssue] = []
        for index, (left_ref, left_start, left_end) in enumerate(intervals):
            for right_ref, right_start, right_end in intervals[index + 1 :]:
                if left_start < right_end and right_start < left_end:
                    issues.append(
                        ValidationIssue(
                            source,
                            "/focal_events",
                            f"unexplained time overlap between {left_ref} and {right_ref}",
                            "EVENT_TIME_OVERLAP",
                        )
                    )
        return issues

    def _annotation_semantics(
        self,
        document: Mapping[str, Any],
        source: str,
        *,
        scenario: Mapping[str, Any] | None,
        require_scenario_context: bool,
    ) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        if require_scenario_context and scenario is None:
            issues.append(
                ValidationIssue(
                    source,
                    "/scenario_id",
                    "annotation scenario_id is not present in the supplied visible scenarios",
                    "SCENARIO_CONTEXT_REQUIRED",
                )
            )

        records = [record for record in document.get("records", []) if isinstance(record, Mapping)]
        seen_ids: set[str] = set()
        by_target: dict[str, dict[str, Any]] = {}
        for index, record in enumerate(records):
            annotation_id = str(record.get("annotation_id", ""))
            if annotation_id in seen_ids:
                issues.append(
                    ValidationIssue(source, f"/records/{index}/annotation_id", "duplicate annotation_id", "DUPLICATE_ANNOTATION")
                )
            seen_ids.add(annotation_id)
            target_ref = str(record.get("target_ref", ""))
            variable = str(record.get("variable", ""))
            if variable in by_target.setdefault(target_ref, {}):
                issues.append(
                    ValidationIssue(
                        source,
                        f"/records/{index}/variable",
                        f"duplicate variable {variable!r} for target {target_ref!r}",
                        "DUPLICATE_TARGET_VARIABLE",
                    )
                )
            by_target[target_ref][variable] = record.get("label")

            for field in ("scenario_id", "annotation_module", "annotator_id", "annotation_round", "manual_version"):
                if record.get(field) != document.get(field):
                    issues.append(
                        ValidationIssue(
                            source,
                            f"/records/{index}/{field}",
                            f"record {field} must match document {field}",
                            "ENVELOPE_MISMATCH",
                        )
                    )
            if scenario is not None and record.get("scenario_version") != scenario.get("scenario_version"):
                issues.append(
                    ValidationIssue(
                        source,
                        f"/records/{index}/scenario_version",
                        "record scenario_version does not match visible scenario",
                        "ENVELOPE_MISMATCH",
                    )
                )

        for index, record in enumerate(records):
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
            issues.extend(self._fact_label_semantics(record, source, index))

        if document.get("annotation_module") == "A":
            issues.extend(self._event_cross_field_semantics(by_target, source))
        if scenario is not None:
            issues.extend(self._evidence_integrity(document, records, scenario, source))
        return issues

    def _event_cross_field_semantics(
        self, by_target: Mapping[str, Mapping[str, Any]], source: str
    ) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        for target_ref, values in by_target.items():
            family = values.get("EVENT_FAMILY")
            if "EVENT_SUBTYPE" in values:
                subtype = values["EVENT_SUBTYPE"]
                if family is None:
                    issues.append(
                        ValidationIssue(source, "/records", f"{target_ref}: EVENT_SUBTYPE requires EVENT_FAMILY in the same document", "CROSS_FIELD_REQUIRED")
                    )
                elif not _valid_subtype(str(family), subtype):
                    issues.append(
                        ValidationIssue(
                            source,
                            "/records",
                            f"{target_ref}: subtype {subtype!r} is not valid for family {family!r}",
                            "SUBTYPE_FAMILY_MISMATCH",
                        )
                    )
            if "LIFECYCLE" in values:
                lifecycle = values["LIFECYCLE"]
                if family is None:
                    issues.append(
                        ValidationIssue(source, "/records", f"{target_ref}: LIFECYCLE requires EVENT_FAMILY in the same document", "CROSS_FIELD_REQUIRED")
                    )
                elif lifecycle not in LIFECYCLES.get(str(family), {"UNKNOWN"}):
                    issues.append(
                        ValidationIssue(
                            source,
                            "/records",
                            f"{target_ref}: lifecycle {lifecycle!r} is not valid for family {family!r}",
                            "LIFECYCLE_FAMILY_MISMATCH",
                        )
                    )
        return issues

    def _fact_label_semantics(
        self, record: Mapping[str, Any], source: str, index: int
    ) -> list[ValidationIssue]:
        variable = record.get("variable")
        label = record.get("label")
        if variable in DATETIME_FACTS and label not in {"UNKNOWN", "N/A"}:
            parsed = _parse_time(label)
            if parsed is None or parsed.tzinfo is None or parsed.utcoffset() is None:
                return [
                    ValidationIssue(
                        source,
                        f"/records/{index}/label",
                        f"{variable} must be UNKNOWN, N/A, or a timezone-aware ISO datetime",
                        "FACT_LABEL_TYPE",
                    )
                ]
        if variable == "PROGRESS" and label not in {"UNKNOWN", "N/A"}:
            if isinstance(label, bool) or not isinstance(label, (int, float)) or not 0 <= label <= 1:
                return [
                    ValidationIssue(
                        source,
                        f"/records/{index}/label",
                        "PROGRESS must be UNKNOWN, N/A, or a number in [0, 1]",
                        "FACT_LABEL_TYPE",
                    )
                ]
        if variable in EFFORT_FACTS and label not in {"UNKNOWN", "N/A", "SMALL", "MEDIUM", "LARGE"}:
            if isinstance(label, bool) or not isinstance(label, (int, float)) or label < 0:
                return [
                    ValidationIssue(
                        source,
                        f"/records/{index}/label",
                        f"{variable} must be non-negative or use an allowed special label",
                        "FACT_LABEL_TYPE",
                    )
                ]
        return []

    def _evidence_integrity(
        self,
        document: Mapping[str, Any],
        records: list[Mapping[str, Any]],
        scenario: Mapping[str, Any],
        source: str,
    ) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        visible_refs, evidence_items, target_refs = _visible_reference_index(scenario)
        module = str(document.get("annotation_module"))
        response_units = {
            str(item.get("response_unit_ref")): item
            for item in scenario.get("bot_response_units", [])
            if isinstance(item, Mapping) and item.get("response_unit_ref")
        }
        for index, record in enumerate(records):
            target_ref = str(record.get("target_ref", ""))
            if target_ref not in target_refs:
                issues.append(
                    ValidationIssue(source, f"/records/{index}/target_ref", f"target_ref {target_ref!r} is not visible in this scenario", "INVALID_TARGET_REFERENCE")
                )
            refs = [str(ref) for ref in record.get("evidence_refs", [])]
            missing = [ref for ref in refs if ref not in visible_refs]
            if missing:
                issues.append(
                    ValidationIssue(source, f"/records/{index}/evidence_refs", f"evidence refs are not visible in this scenario: {missing}", "INVALID_EVIDENCE_REFERENCE")
                )

            label = record.get("label")
            if module == "B" and label not in {"NO_EVIDENCE", "AMBIGUOUS"}:
                if not refs:
                    issues.append(
                        ValidationIssue(source, f"/records/{index}/evidence_refs", "directional appraisal annotation requires at least one evidence_ref", "MISSING_EVIDENCE_REFERENCE")
                    )
                participant_items = [
                    evidence_items[ref]
                    for ref in refs
                    if ref in evidence_items and evidence_items[ref].get("speaker") == "PARTICIPANT"
                ]
                span = str(record.get("evidence_span", ""))
                if refs and (not participant_items or not any(_span_matches(span, str(item.get("text", ""))) for item in participant_items)):
                    issues.append(
                        ValidationIssue(source, f"/records/{index}/evidence_span", "evidence_span is not present in the cited participant evidence", "EVIDENCE_SPAN_MISMATCH")
                    )

            if module == "C":
                response = response_units.get(target_ref)
                sent_at = _parse_time(response.get("sent_at")) if response else None
                for ref in refs:
                    if ref == target_ref:
                        continue
                    item = evidence_items.get(ref)
                    known_at = _parse_time(item.get("known_at")) if item else None
                    if item is None or sent_at is None or known_at is None or known_at > sent_at:
                        issues.append(
                            ValidationIssue(
                                source,
                                f"/records/{index}/evidence_refs",
                                f"Module C evidence {ref!r} was not known when {target_ref!r} was sent",
                                "FUTURE_SUPPORT_EVIDENCE",
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


def _parse_clock(value: Any) -> time | None:
    if not isinstance(value, str):
        return None
    try:
        return time.fromisoformat(value)
    except ValueError:
        return None


def _valid_subtype(family: str, label: Any) -> bool:
    if label in {"UNKNOWN", "N/A"}:
        return True
    if isinstance(label, str) and label.startswith("OTHER:") and label.partition(":")[2].strip():
        return True
    return label in EVENT_SUBTYPES.get(family, set())


def _normalized_evidence_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    without_punctuation = "".join(
        " " if unicodedata.category(character).startswith("P") else character
        for character in normalized
    )
    return re.sub(r"\s+", "", without_punctuation)


def _span_matches(span: str, evidence_text: str) -> bool:
    normalized_span = _normalized_evidence_text(span)
    return bool(normalized_span) and normalized_span in _normalized_evidence_text(evidence_text)


def _visible_reference_index(
    scenario: Mapping[str, Any],
) -> tuple[set[str], dict[str, Mapping[str, Any]], set[str]]:
    visible_refs = {str(ref) for ref in scenario.get("source_refs", [])}
    evidence_items: dict[str, Mapping[str, Any]] = {}
    target_refs: set[str] = set()
    collections = (
        ("recurring_course_context", "course_ref", False),
        ("recent_context", "evidence_ref", False),
        ("current_tasks", "event_ref", True),
        ("focal_events", "event_ref", True),
        ("observed_conversation_evidence", "evidence_ref", False),
        ("bot_response_units", "response_unit_ref", True),
    )
    for collection_name, ref_field, is_target in collections:
        for item in scenario.get(collection_name, []):
            if not isinstance(item, Mapping) or not item.get(ref_field):
                continue
            ref = str(item[ref_field])
            visible_refs.add(ref)
            evidence_items[ref] = item
            source_ref = item.get("source_ref")
            if isinstance(source_ref, str):
                visible_refs.add(source_ref)
            if is_target:
                target_refs.add(ref)
    return visible_refs, evidence_items, target_refs


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
