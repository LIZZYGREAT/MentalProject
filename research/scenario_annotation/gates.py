"""Evidence-backed gates for Stage 1 progression.

Gate evaluation is deliberately strict: absent independent annotations or human
revision decisions produce FAIL, never a synthetic PASS.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Callable, Mapping

from .annotation_contract import check_submission_integrity
from .analysis_manifest import verify_analysis_manifest_current
from .artifact_fingerprint import sha256_file
from .analysis.common import load_annotation_documents
from .loader import load_jsonl
from .reference_set import expected_reference_keys
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
    input_manifest_sha256: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "gate": self.gate,
            "status": self.status,
            "evaluated_at": self.evaluated_at,
            "checks": [asdict(check) for check in self.checks],
            "blocking_reasons": list(self.blocking_reasons),
            "input_manifest_sha256": self.input_manifest_sha256,
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
    assignments_root: str | Path | None = None,
    pair_design_path: str | Path | None = None,
    quality_thresholds_path: str | Path | None = None,
) -> GateResult:
    manual_path = Path(manual_path)
    scenarios_path = Path(scenarios_path)
    coverage_path = Path(coverage_path)
    analysis_dir = Path(analysis_dir)
    revision_log_path = Path(revision_log_path)
    assignments_root = Path(
        assignments_root
        or scenarios_path.parent.parent / "assignments" / "round_calibration"
    )
    pair_design_path = Path(pair_design_path or scenarios_path.parent.parent / "hidden" / "pair_design.jsonl")
    quality_thresholds_path = Path(quality_thresholds_path or Path(__file__).with_name("settings") / "quality_thresholds_v1.json")

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
        scenarios = {str(row["scenario_id"]): row for row in load_jsonl(scenarios_path)}
        documents = load_annotation_documents(
            annotations_dir,
            validate=True,
            scenarios=scenarios,
            require_scenario_context=True,
        )
        integrity = check_submission_integrity(
            assignments_root, documents, annotators=PRIMARY_ANNOTATORS
        )
        return integrity.ok, integrity.detail()

    def analysis_check() -> tuple[bool, str]:
        required = {
            "field_metrics.csv",
            "semantic_violation_rates.json",
            "artifact_validity.json",
            "disagreement_report.md",
            "scenario_annotation_report.md",
            "analysis_manifest.json",
        }
        present = {path.name for path in analysis_dir.iterdir()} if analysis_dir.exists() else set()
        missing = sorted(required - present)
        if missing:
            return False, f"missing_analysis_outputs={missing}"
        verify_analysis_manifest_current(
            analysis_dir / "analysis_manifest.json",
            annotations_dir=annotations_dir,
            scenario_paths=[scenarios_path],
            coverage_path=coverage_path,
            pair_design_path=pair_design_path,
            quality_thresholds_path=quality_thresholds_path,
        )
        return True, "analysis outputs complete and input manifest is current"

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
        input_manifest_sha256=(
            sha256_file(analysis_dir / "analysis_manifest.json")
            if (analysis_dir / "analysis_manifest.json").exists()
            else None
        ),
    )


def write_gate_result(path: str | Path, result: GateResult) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(result.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def evaluate_semantic_reliability(
    *,
    main_scenarios_path: str | Path,
    edge_scenarios_path: str | Path,
    annotations_dir: str | Path,
    analysis_dir: str | Path,
    quality_thresholds_path: str | Path | None = None,
    assignments_root: str | Path | None = None,
    coverage_path: str | Path | None = None,
    pair_design_path: str | Path | None = None,
) -> GateResult:
    main_scenarios_path = Path(main_scenarios_path)
    edge_scenarios_path = Path(edge_scenarios_path)
    analysis_dir = Path(analysis_dir)
    quality_thresholds_path = Path(
        quality_thresholds_path
        or Path(__file__).with_name("settings") / "quality_thresholds_v1.json"
    )
    assignments_root = Path(
        assignments_root
        or main_scenarios_path.parent.parent / "assignments" / "round_validation"
    )
    coverage_path = Path(coverage_path or main_scenarios_path.parent.parent / "hidden" / "coverage_tags.jsonl")
    pair_design_path = Path(pair_design_path or main_scenarios_path.parent.parent / "hidden" / "pair_design.jsonl")

    def corpus_check() -> tuple[bool, str]:
        main = Validator().validate_paths([main_scenarios_path], "scenario")
        edge = Validator().validate_paths([edge_scenarios_path], "scenario")
        passed = main.ok and edge.ok and main.checked == 72 and edge.checked == 24
        return passed, f"main={main.checked}/72 valid={main.ok}; edge={edge.checked}/24 valid={edge.ok}"

    def annotation_check() -> tuple[bool, str]:
        scenarios = {
            str(row["scenario_id"]): row
            for path in (main_scenarios_path, edge_scenarios_path)
            for row in load_jsonl(path)
        }
        documents = load_annotation_documents(
            annotations_dir,
            validate=True,
            scenarios=scenarios,
            require_scenario_context=True,
        )
        versions = {str(document["manual_version"]) for document in documents}
        rounds = {str(document["annotation_round"]) for document in documents}
        integrity = check_submission_integrity(
            assignments_root, documents, annotators=PRIMARY_ANNOTATORS
        )
        passed = integrity.ok and versions == {"1.0"} and rounds <= {"VALIDATION", "REANNOTATION"}
        return passed, integrity.detail() + (
            f"; manual_versions={sorted(versions)}; rounds={sorted(rounds)}"
        )

    def metrics_check() -> tuple[bool, str]:
        metrics_path = analysis_dir / "field_metrics.csv"
        import csv

        with metrics_path.open("r", encoding="utf-8", newline="") as stream:
            metrics = list(csv.DictReader(stream))
        blocked = [
            row["field"]
            for row in metrics
            if row.get("freeze_status") in {"RECONSIDER", "INSUFFICIENT_DATA"}
        ]
        passed = bool(metrics) and not blocked
        return passed, f"field_count={len(metrics)}; blocked_fields={blocked}"

    def violation_check() -> tuple[bool, str]:
        rates = json.loads((analysis_dir / "semantic_violation_rates.json").read_text(encoding="utf-8"))
        settings_result = Validator().validate_paths([quality_thresholds_path], "quality-thresholds")
        if not settings_result.ok:
            return False, "invalid quality threshold settings: " + "; ".join(
                str(issue) for issue in settings_result.issues
            )
        settings = json.loads(quality_thresholds_path.read_text(encoding="utf-8"))
        passed, exceeded = evaluate_violation_thresholds(rates, settings)
        return passed, (
            f"settings_version={settings['settings_version']}; exceeded_thresholds={exceeded}"
        )

    def artifact_validity_check() -> tuple[bool, str]:
        validity = json.loads((analysis_dir / "artifact_validity.json").read_text(encoding="utf-8"))
        passed = validity.get("status") == "PASS" and all(
            value == "PASS" for value in validity.get("checks", {}).values()
        )
        return passed, f"artifact_validity={validity.get('status')}; checks={validity.get('checks', {})}"

    def outputs_check() -> tuple[bool, str]:
        required = {
            "field_metrics.csv",
            "semantic_violation_rates.json",
            "artifact_validity.json",
            "orthogonality.jsonl",
            "disagreement_queue.jsonl",
            "disagreement_report.md",
            "scenario_annotation_report.md",
            "analysis_manifest.json",
        }
        present = {path.name for path in analysis_dir.iterdir()} if analysis_dir.exists() else set()
        missing = sorted(required - present)
        if missing:
            return False, f"missing_analysis_outputs={missing}"
        verify_analysis_manifest_current(
            analysis_dir / "analysis_manifest.json",
            annotations_dir=annotations_dir,
            scenario_paths=[main_scenarios_path, edge_scenarios_path],
            coverage_path=coverage_path,
            pair_design_path=pair_design_path,
            quality_thresholds_path=quality_thresholds_path,
        )
        return True, "analysis outputs complete and input manifest is current"

    checks = (
        _check("formal_corpus_schema_and_count", corpus_check),
        _check("blind_independent_annotations", annotation_check),
        _check("field_level_reliability", metrics_check),
        _check("artifact_validity", artifact_validity_check),
        _check("semantic_violation_rates", violation_check),
        _check("validation_analysis_outputs", outputs_check),
    )
    blockers = tuple(check.detail for check in checks if not check.passed)
    return GateResult(
        gate="SEMANTIC_RELIABILITY",
        status="PASS" if not blockers else "FAIL",
        evaluated_at=datetime.now(timezone.utc).isoformat(),
        checks=checks,
        blocking_reasons=blockers,
        input_manifest_sha256=(
            sha256_file(analysis_dir / "analysis_manifest.json")
            if (analysis_dir / "analysis_manifest.json").exists()
            else None
        ),
    )


def evaluate_violation_thresholds(
    rates: Mapping[str, Mapping[str, Any]], settings: Mapping[str, Any]
) -> tuple[bool, dict[str, dict[str, float | str]]]:
    exceeded: dict[str, dict[str, float | str]] = {}
    groups = (("SEMANTIC_MISUNDERSTANDING", settings.get("semantic_misunderstanding_max_rates", {})),)
    for category, thresholds in groups:
        for metric, maximum in thresholds.items():
            actual = float(rates.get(metric, {}).get("rate", 0.0))
            if actual > float(maximum):
                exceeded[str(metric)] = {
                    "category": category,
                    "actual": actual,
                    "maximum": float(maximum),
                }
    return not exceeded, exceeded


def evaluate_representation_freeze(
    *,
    gate_a_path: str | Path,
    gate_b_path: str | Path,
    manual_path: str | Path,
    main_scenarios_path: str | Path,
    edge_scenarios_path: str | Path,
    gold_path: str | Path,
    revision_log_path: str | Path,
    gate_a_analysis_manifest_path: str | Path | None = None,
    gate_b_analysis_manifest_path: str | Path | None = None,
) -> GateResult:
    gate_a_path = Path(gate_a_path)
    gate_b_path = Path(gate_b_path)
    manual_path = Path(manual_path)
    main_scenarios_path = Path(main_scenarios_path)
    edge_scenarios_path = Path(edge_scenarios_path)
    gold_path = Path(gold_path)
    revision_log_path = Path(revision_log_path)
    gate_a_analysis_manifest_path = Path(gate_a_analysis_manifest_path) if gate_a_analysis_manifest_path else None
    gate_b_analysis_manifest_path = Path(gate_b_analysis_manifest_path) if gate_b_analysis_manifest_path else None

    def prior_gates_check() -> tuple[bool, str]:
        values = [json.loads(path.read_text(encoding="utf-8")) for path in (gate_a_path, gate_b_path)]
        statuses = {value.get("gate"): value.get("status") for value in values}
        manifest_paths = (gate_a_analysis_manifest_path, gate_b_analysis_manifest_path)
        hashes_match = all(
            path is not None
            and path.exists()
            and value.get("input_manifest_sha256") == sha256_file(path)
            for value, path in zip(values, manifest_paths)
        )
        passed = statuses == {"MANUAL_READY": "PASS", "SEMANTIC_RELIABILITY": "PASS"} and hashes_match
        return passed, f"prior_gate_statuses={statuses}; input_manifest_hashes_match={hashes_match}"

    def manual_check() -> tuple[bool, str]:
        text = manual_path.read_text(encoding="utf-8")
        passed = "v1.0" in text and "candidate" not in text.lower()
        return passed, f"final_manual={manual_path}; final_marker={passed}"

    def gold_check() -> tuple[bool, str]:
        result = Validator().validate_paths([gold_path], "reference-record")
        rows = load_jsonl(gold_path)
        scenarios = [
            row
            for path in (main_scenarios_path, edge_scenarios_path)
            for row in load_jsonl(path)
        ]
        expected = expected_reference_keys(scenarios)
        actual = {
            (str(row["scenario_id"]), str(row["target_ref"]), str(row["variable"]))
            for row in rows
        }
        missing = sorted(expected - actual)
        unexpected = sorted(actual - expected)
        passed = result.ok and len(scenarios) == 96 and not missing and not unexpected
        return passed, (
            f"schema_valid={result.ok}; formal_scenarios={len(scenarios)}; "
            f"expected_keys={len(expected)}; actual_keys={len(actual)}; "
            f"missing_reference_keys={missing}; unexpected_reference_keys={unexpected}"
        )

    def decision_check() -> tuple[bool, str]:
        result = Validator().validate_paths([revision_log_path], "revision-log")
        rows = load_jsonl(revision_log_path)
        unresolved_revision = [row["construct"] for row in rows if row.get("decision") == "REVISE"]
        constructs = {str(row["construct"]) for row in rows}
        missing = sorted(MANUAL_GATE_CONSTRUCTS - constructs)
        passed = result.ok and not unresolved_revision and not missing
        return passed, f"schema_valid={result.ok}; unresolved_revise={unresolved_revision}; missing={missing}"

    checks = (
        _check("prior_gates", prior_gates_check),
        _check("coding_manual_v1_final", manual_check),
        _check("adjudicated_reference_set", gold_check),
        _check("final_construct_decisions", decision_check),
    )
    blockers = tuple(check.detail for check in checks if not check.passed)
    return GateResult(
        gate="REPRESENTATION_FREEZE",
        status="PASS" if not blockers else "FAIL",
        evaluated_at=datetime.now(timezone.utc).isoformat(),
        checks=checks,
        blocking_reasons=blockers,
        input_manifest_sha256=(
            sha256_file(gate_b_analysis_manifest_path)
            if gate_b_analysis_manifest_path and gate_b_analysis_manifest_path.exists()
            else None
        ),
    )
