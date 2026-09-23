"""Evidence-backed validation of Stage 1 analysis inputs."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from ..annotation_contract import check_submission_integrity
from ..loader import ArtifactLoadError, load_json, load_jsonl
from ..validation import ValidationIssue, ValidationResult, Validator
from .common import annotation_files


_ANNOTATION_TYPES = {
    "A": "event-annotation",
    "B": "appraisal-annotation",
    "C": "bot-annotation",
}
_EVIDENCE_CODES = {
    "INVALID_EVIDENCE_REFERENCE",
    "MISSING_EVIDENCE_REFERENCE",
    "EVIDENCE_SPAN_MISMATCH",
    "INVALID_TARGET_REFERENCE",
    "FUTURE_SUPPORT_EVIDENCE",
}


def _issue(source: str, message: str, code: str) -> ValidationIssue:
    return ValidationIssue(source, "", message, code)


def _load_objects(path: Path) -> list[dict[str, Any]]:
    if path.suffix == ".jsonl":
        return load_jsonl(path)
    value = load_json(path)
    if not isinstance(value, dict):
        raise ArtifactLoadError(f"cannot load {path}: expected top-level object")
    return [value]


def _check_details(checked: int, issues: Iterable[ValidationIssue]) -> dict[str, Any]:
    return {
        "checked": checked,
        "issues": [
            {
                "source": issue.source,
                "path": issue.path,
                "code": issue.code,
                "message": issue.message,
            }
            for issue in issues
        ],
    }


def _status(checked: int, issues: Iterable[ValidationIssue]) -> str:
    if any(True for _ in issues):
        return "FAIL"
    return "PASS" if checked > 0 else "NOT_EVALUATED"


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _extend_result(
    results: list[ValidationResult],
    extra_issues: Iterable[ValidationIssue] = (),
) -> tuple[int, list[ValidationIssue]]:
    issues = [issue for result in results for issue in result.issues]
    issues.extend(extra_issues)
    return sum(result.checked for result in results), issues


def _assignment_issues(
    assignments_root: Path,
    documents: list[dict[str, Any]],
) -> tuple[int, list[ValidationIssue]]:
    manifests = sorted(assignments_root.glob("*/manifest.json"))
    issues: list[ValidationIssue] = []
    if not manifests:
        return 0, [_issue(str(assignments_root), "no assignment manifests found", "NO_ASSIGNMENTS")]
    unsafe = [path for path in manifests if path.is_symlink() or not _is_within(path, assignments_root)]
    if unsafe:
        issues.extend(
            _issue(str(path), "assignment manifest resolves outside its root", "ASSIGNMENT_PATH_INVALID")
            for path in unsafe
        )
        return 0, issues
    try:
        integrity = check_submission_integrity(assignments_root, documents)
        if not integrity.ok:
            issues.append(_issue(str(assignments_root), integrity.detail(), "SUBMISSION_INCOMPLETE"))
    except (ArtifactLoadError, OSError, ValueError, KeyError, TypeError) as exc:
        issues.append(_issue(str(assignments_root), str(exc), "SUBMISSION_CHECK_ERROR"))
    return len(manifests), issues


def evaluate_artifact_validity(
    *,
    scenarios_paths: Iterable[str | Path],
    annotations_dir: str | Path,
    coverage_path: str | Path,
    pairs_path: str | Path,
    assignments_root: str | Path | None = None,
    anchor_reference_path: str | Path | None = None,
    anchor_review_decisions_path: str | Path | None = None,
    manual_path: str | Path | None = None,
) -> dict[str, Any]:
    """Validate every input that can affect Stage 1 analysis.

    A check without a configured input is reported as ``NOT_EVALUATED`` and
    prevents an overall PASS. Validator issues are retained in the output so a
    failed artifact can be inspected without rerunning the pipeline.
    """
    validator = Validator()
    scenario_paths = [Path(path) for path in scenarios_paths]
    scenario_results = [validator.validate_paths([path], "scenario") for path in scenario_paths]
    scenario_rows: list[dict[str, Any]] = []
    scenario_extra_issues: list[ValidationIssue] = []
    for path in scenario_paths:
        try:
            scenario_rows.extend(_load_objects(path))
        except (ArtifactLoadError, OSError) as exc:
            scenario_extra_issues.append(_issue(str(path), str(exc), "SCENARIO_LOAD"))
    scenarios: dict[str, dict[str, Any]] = {}
    seen_scenarios: set[str] = set()
    for scenario in scenario_rows:
        scenario_id = str(scenario.get("scenario_id", ""))
        if scenario_id in seen_scenarios:
            scenario_extra_issues.append(
                _issue(
                    scenario_id,
                    "scenario_id occurs more than once in the supplied corpus",
                    "DUPLICATE_SCENARIO_ID",
                )
            )
        seen_scenarios.add(scenario_id)
        if scenario_id:
            scenarios[scenario_id] = scenario
    if not scenario_rows:
        scenario_extra_issues.append(
            _issue("scenario corpus", "no scenarios were loaded", "NO_SCENARIOS")
        )
    scenario_checked = sum(result.checked for result in scenario_results)

    annotation_results: list[ValidationResult] = []
    annotation_extra_issues: list[ValidationIssue] = []
    documents: list[dict[str, Any]] = []
    annotation_paths = annotation_files(annotations_dir)
    annotation_document_count = 0
    for path in annotation_paths:
        try:
            candidates = [
                value for value in _load_objects(path)
                if isinstance(value, dict) and "annotation_module" in value
            ]
        except (ArtifactLoadError, OSError) as exc:
            annotation_extra_issues.append(_issue(str(path), str(exc), "ANNOTATION_LOAD"))
            continue
        if not candidates:
            continue
        documents.extend(candidates)
        annotation_document_count += len(candidates)
        modules = {str(candidate.get("annotation_module")) for candidate in candidates}
        if len(modules) != 1:
            annotation_extra_issues.append(
                _issue(
                    str(path),
                    f"annotation file mixes modules {sorted(modules)}",
                    "MIXED_ANNOTATION_MODULES",
                )
            )
        module = next(iter(modules))
        artifact_type = _ANNOTATION_TYPES.get(module)
        if artifact_type is None:
            annotation_extra_issues.append(
                _issue(
                    str(path),
                    f"unknown annotation module {module!r}",
                    "UNKNOWN_ANNOTATION_MODULE",
                )
            )
            continue
        annotation_results.append(
            validator.validate_paths(
                [path],
                artifact_type,
                scenarios=scenarios,
                require_scenario_context=True,
            )
        )
    if annotation_document_count == 0:
        annotation_extra_issues.append(
            _issue(str(annotations_dir), "no annotation documents were loaded", "NO_ANNOTATIONS")
        )
    annotation_checked = sum(result.checked for result in annotation_results)

    manual_issues: list[ValidationIssue] = []
    manual_versions = {str(document.get("manual_version", "")) for document in documents}
    manual_checks = 0
    for document in documents:
        version = str(document.get("manual_version", ""))
        current_manual = Path(
            manual_path
            or Path(__file__).parents[1] / "manuals" / f"coding_manual_v{version}.md"
        )
        manual_checks += 1
        try:
            digest = hashlib.sha256(current_manual.read_bytes()).hexdigest()
        except OSError as exc:
            manual_issues.append(_issue(str(current_manual), str(exc), "MANUAL_MISSING"))
            continue
        if document.get("manual_sha256") != digest:
            manual_issues.append(
                _issue(
                    str(document.get("scenario_id", "")),
                    "annotation manual hash does not match current manual content",
                    "ANNOTATION_MANUAL_HASH_MISMATCH",
                )
            )
        runner = document.get("runner_provenance")
        if isinstance(runner, dict) and runner.get("manual_sha256") != document.get("manual_sha256"):
            manual_issues.append(
                _issue(
                    str(document.get("scenario_id", "")),
                    "runner provenance manual hash differs from the annotation artifact",
                    "RUNNER_MANUAL_HASH_MISMATCH",
                )
            )
    if len(manual_versions) > 1:
        manual_issues.append(_issue(str(annotations_dir), "annotations use multiple manual versions", "MIXED_MANUAL_VERSIONS"))

    pair_result = validator.validate_paths([pairs_path], "pair-design", scenarios=scenarios)
    pair_extra_issues: list[ValidationIssue] = []
    if pair_result.checked == 0 and not pair_result.issues:
        pair_extra_issues.append(
            _issue(str(pairs_path), "no pair designs were loaded", "NO_PAIR_DESIGNS")
        )

    coverage_result = validator.validate_paths([coverage_path], "coverage-tags")
    coverage_extra_issues: list[ValidationIssue] = []
    coverage_rows: list[dict[str, Any]] = []
    try:
        coverage_rows = load_jsonl(coverage_path)
    except (ArtifactLoadError, OSError) as exc:
        coverage_extra_issues.append(_issue(str(coverage_path), str(exc), "COVERAGE_LOAD"))
    coverage_ids = [str(row.get("scenario_id", "")) for row in coverage_rows]
    if len(set(coverage_ids)) != len(coverage_ids):
        coverage_extra_issues.append(
            _issue(
                str(coverage_path),
                "scenario_id occurs more than once",
                "DUPLICATE_COVERAGE_SCENARIO",
            )
        )
    if set(coverage_ids) != set(scenarios):
        coverage_extra_issues.append(
            _issue(
                str(coverage_path),
                "coverage scenario IDs differ from corpus: "
                f"missing={sorted(set(scenarios) - set(coverage_ids))}; "
                f"extra={sorted(set(coverage_ids) - set(scenarios))}",
                "COVERAGE_SCENARIO_MISMATCH",
            )
        )
    if not coverage_rows:
        coverage_extra_issues.append(
            _issue(str(coverage_path), "no coverage tags were loaded", "NO_COVERAGE_TAGS")
        )

    if anchor_reference_path is None:
        anchor_result = ValidationResult(
            0,
            (_issue("anchor reference", "anchor_reference_path was not provided", "ANCHOR_ROOT_NOT_CONFIGURED"),),
        )
    else:
        anchor_result = validator.validate_paths([anchor_reference_path], "anchor-reference")
        if anchor_result.checked == 0 and not anchor_result.issues:
            anchor_result = ValidationResult(
                0,
                (_issue(str(anchor_reference_path), "no design anchors were loaded", "NO_ANCHORS"),),
            )
    review_not_required = anchor_review_decisions_path is None or not Path(anchor_review_decisions_path).exists()
    if review_not_required:
        review_result = ValidationResult(0, ())
    else:
        review_result = validator.validate_paths([anchor_review_decisions_path], "anchor-review")

    assignment_checked = 0
    assignment_issues: list[ValidationIssue] = []
    assignment_not_configured = assignments_root is None
    if assignments_root is not None:
        assignment_checked, assignment_issues = _assignment_issues(
            Path(assignments_root), documents
        )
    else:
        assignment_issues.append(
            _issue(
                "assignment root",
                "assignments_root was not provided",
                "ASSIGNMENTS_ROOT_NOT_CONFIGURED",
            )
        )

    scenario_validation_checked, scenario_validation_issues = _extend_result(
        scenario_results, scenario_extra_issues
    )
    annotation_validation_checked, annotation_validation_issues = _extend_result(
        annotation_results, annotation_extra_issues
    )
    pair_checked, pair_validation_issues = _extend_result([pair_result], pair_extra_issues)
    coverage_checked, coverage_validation_issues = _extend_result(
        [coverage_result], coverage_extra_issues
    )
    anchor_checked, anchor_validation_issues = anchor_result.checked, list(anchor_result.issues)

    hidden_issues = [
        issue for issue in scenario_validation_issues if issue.code == "HIDDEN_METADATA"
    ]
    evidence_issues = [
        issue for issue in annotation_validation_issues if issue.code in _EVIDENCE_CODES
    ]
    known_at_issues = [
        issue for issue in scenario_validation_issues + annotation_validation_issues
        if issue.code in {"FUTURE_KNOWLEDGE", "FUTURE_SUPPORT_EVIDENCE"}
    ]
    exposure_issues = [
        issue for issue in scenario_validation_issues + annotation_validation_issues
        if issue.code in {"EXPOSURE_DOUBLE_ENCODING", "MISSING_PARTIAL_BASIS"}
    ]
    check_inputs = {
        "scenario_validator": (scenario_validation_checked, scenario_validation_issues),
        "annotation_validator": (annotation_validation_checked, annotation_validation_issues),
        "pair_design_validator": (pair_checked, pair_validation_issues),
        "coverage_validator": (coverage_checked, coverage_validation_issues),
        "anchor_reference_validator": (anchor_checked, anchor_validation_issues),
        "manual_provenance": (manual_checks, manual_issues),
        "anchor_review_decision_validator": (
            max(review_result.checked, 1) if review_not_required else review_result.checked,
            list(review_result.issues),
        ),
        "assignment_submission_integrity": (assignment_checked, assignment_issues),
        "hidden_metadata": (scenario_checked, hidden_issues),
        "evidence_reference_integrity": (annotation_checked, evidence_issues),
        "known_at_checks": (scenario_checked + annotation_checked, known_at_issues),
        "exposure_structural_invariant": (scenario_checked + annotation_checked, exposure_issues),
    }
    checks = {
        name: (
            "NOT_EVALUATED"
            if name == "assignment_submission_integrity" and assignment_not_configured
            else "PASS"
            if name == "anchor_review_decision_validator" and review_not_required
            else _status(checked, issues)
        )
        for name, (checked, issues) in check_inputs.items()
    }
    details = {
        name: _check_details(checked, issues)
        for name, (checked, issues) in check_inputs.items()
    }
    return {
        "status": "PASS" if all(status == "PASS" for status in checks.values()) else "FAIL",
        "validated_at": datetime.now(timezone.utc).isoformat(),
        "checks": checks,
        "details": details,
    }
