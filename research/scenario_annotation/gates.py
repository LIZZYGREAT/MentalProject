"""Evidence-backed gates for Stage 1 progression.

Gate evaluation is deliberately strict: absent independent annotations or human
revision decisions produce FAIL, never a synthetic PASS.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Callable

from .analysis.common import load_annotation_documents
from .loader import load_jsonl
from .validation import Validator


PRIMARY_ANNOTATORS = {"AI-A", "AI-B", "AI-C", "Human"}
MANUAL_GATE_CONSTRUCTS = {
    "EVENT_FAMILY",
    "LIFECYCLE",
    "OBLIGATION_EXISTS",
    "D_POT",
    "U_CONTEXT",
    "D_S",
    "R_POT",
    "M_CONTEXT",
    "C_EXEC",
    "IMPORTANCE",
    "C_OUT",
    "U_PERC",
    "F_REC",
    "SUPPORT_GATE",
    "RELEVANCE",
}
REQUIRED_CALIBRATION_TAGS = {
    "SCHEDULED_VS_REALIZED",
    "ACTUAL_INTERVAL",
    "FRACTION_ONLY",
    "MISSED_NOT_OBLIGATION",
    "OBLIGATION",
    "RECOVERY_TRAP",
    "F_REC",
    "C_EXEC",
    "IMPORTANCE",
    "C_OUT",
    "U_CONTEXT",
    "U_PERC",
    "TASK_HELP",
    "COPING_SUPPORT",
    "FUTURE_LEAKAGE",
}


@dataclass(frozen=True)
class GateCheck:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class GateResult:
    gate: str
    status: str
    evaluated_at: str
    checks: tuple[GateCheck, ...]
    blocking_reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "gate": self.gate,
            "status": self.status,
            "evaluated_at": self.evaluated_at,
            "checks": [asdict(check) for check in self.checks],
            "blocking_reasons": list(self.blocking_reasons),
        }


def _check(name: str, operation: Callable[[], tuple[bool, str]]) -> GateCheck:
    try:
        passed, detail = operation()
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return GateCheck(name, False, str(exc))
    return GateCheck(name, passed, detail)


def evaluate_manual_ready(
    *,
    manual_path: str | Path,
    scenarios_path: str | Path,
    coverage_path: str | Path,
    annotations_dir: str | Path,
    analysis_dir: str | Path,
    revision_log_path: str | Path,
) -> GateResult:
    manual_path = Path(manual_path)
    scenarios_path = Path(scenarios_path)
    coverage_path = Path(coverage_path)
    analysis_dir = Path(analysis_dir)
    revision_log_path = Path(revision_log_path)

    def manual_check() -> tuple[bool, str]:
        text = manual_path.read_text(encoding="utf-8")
        passed = "v1.0" in text and "Calibration draft" not in text
        return passed, f"manual={manual_path}; version marker v1.0={passed}"

    def scenario_check() -> tuple[bool, str]:
        result = Validator().validate_paths([scenarios_path], "scenario")
        passed = result.ok and result.checked == 24
        return passed, f"schema_valid={result.ok}; scenario_count={result.checked}; expected=24"

    def coverage_check() -> tuple[bool, str]:
        rows = load_jsonl(coverage_path)
        tags = {str(tag) for row in rows for tag in row.get("coverage_tags", [])}
        missing = sorted(REQUIRED_CALIBRATION_TAGS - tags)
        return not missing, f"missing_required_tags={missing}"

    def annotation_check() -> tuple[bool, str]:
        documents = load_annotation_documents(annotations_dir, validate=True)
        annotators = {str(document["annotator_id"]) for document in documents}
        missing = sorted(PRIMARY_ANNOTATORS - annotators)
        return not missing, f"annotators={sorted(annotators)}; missing={missing}"

    def analysis_check() -> tuple[bool, str]:
        required = {
            "field_metrics.csv",
            "critical_violation_rates.json",
            "disagreement_report.md",
            "scenario_annotation_report.md",
        }
        present = {path.name for path in analysis_dir.iterdir()} if analysis_dir.exists() else set()
        missing = sorted(required - present)
        return not missing, f"missing_analysis_outputs={missing}"

    def revision_check() -> tuple[bool, str]:
        result = Validator().validate_paths([revision_log_path], "revision-log")
        rows = load_jsonl(revision_log_path)
        constructs = {str(row["construct"]) for row in rows if row.get("status") == "RESOLVED"}
        missing = sorted(MANUAL_GATE_CONSTRUCTS - constructs)
        passed = result.ok and not missing
        return passed, f"schema_valid={result.ok}; missing_construct_decisions={missing}"

    checks = (
        _check("coding_manual_v1_candidate", manual_check),
        _check("calibration_scenario_schema_and_count", scenario_check),
        _check("critical_boundary_coverage", coverage_check),
        _check("independent_primary_annotators", annotation_check),
        _check("calibration_analysis_outputs", analysis_check),
        _check("resolved_revision_decisions", revision_check),
    )
    blockers = tuple(check.detail for check in checks if not check.passed)
    return GateResult(
        gate="MANUAL_READY",
        status="PASS" if not blockers else "FAIL",
        evaluated_at=datetime.now(timezone.utc).isoformat(),
        checks=checks,
        blocking_reasons=blockers,
    )


def write_gate_result(path: str | Path, result: GateResult) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(result.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
