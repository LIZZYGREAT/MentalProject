"""Create an immutable representation manifest only after all Stage 1 gates pass."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
from typing import Any

from .gates import evaluate_representation_freeze
from .loader import load_jsonl
from .validation import Validator


class FreezeBlockedError(RuntimeError):
    pass


def _git_revision(repository_root: Path) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def freeze_representation(
    *,
    repository_root: str | Path,
    gate_a_path: str | Path,
    gate_b_path: str | Path,
    manual_path: str | Path,
    main_scenarios_path: str | Path,
    edge_scenarios_path: str | Path,
    gold_path: str | Path,
    revision_log_path: str | Path,
    field_metrics_path: str | Path,
    critical_violation_report_path: str | Path,
    output_path: str | Path,
    representation_version: str = "1.0",
    manual_version: str = "1.0",
    scenario_version: str = "1.0",
    gold_version: str = "1.0",
) -> dict[str, Any]:
    gate = evaluate_representation_freeze(
        gate_a_path=gate_a_path,
        gate_b_path=gate_b_path,
        manual_path=manual_path,
        main_scenarios_path=main_scenarios_path,
        edge_scenarios_path=edge_scenarios_path,
        gold_path=gold_path,
        revision_log_path=revision_log_path,
    )
    if gate.status != "PASS":
        raise FreezeBlockedError("representation freeze blocked: " + " | ".join(gate.blocking_reasons))
    decisions = load_jsonl(revision_log_path)
    by_decision = {
        decision: sorted(str(row["construct"]) for row in decisions if row["decision"] == decision)
        for decision in ("KEEP", "REVISE", "SIMPLIFY", "DROP")
    }
    manifest = {
        "representation_version": representation_version,
        "frozen_at": datetime.now(timezone.utc).isoformat(),
        "manual_version": manual_version,
        "scenario_bank_version": scenario_version,
        "gold_set_version": gold_version,
        "schema_versions": {
            "scenario": "1.0",
            "event_annotation": "1.0",
            "appraisal_annotation": "1.0",
            "bot_annotation": "1.0",
            "adjudication": "1.0",
        },
        "included_constructs": sorted(by_decision["KEEP"] + by_decision["SIMPLIFY"]),
        "revised_constructs": by_decision["REVISE"],
        "simplified_constructs": by_decision["SIMPLIFY"],
        "dropped_constructs": by_decision["DROP"],
        "field_metrics_ref": str(Path(field_metrics_path).as_posix()),
        "critical_violation_report_ref": str(Path(critical_violation_report_path).as_posix()),
        "known_open_questions": [],
        "git_revision": _git_revision(Path(repository_root)),
    }
    # Validate through the schema directly without writing an invalid manifest.
    from jsonschema import Draft202012Validator, FormatChecker

    schema = Validator()._schema("freeze-manifest")
    errors = list(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(manifest))
    if errors:
        raise FreezeBlockedError("generated freeze manifest failed schema validation")
    output = Path(output_path)
    if output.exists():
        raise FreezeBlockedError(f"refusing to overwrite existing freeze manifest: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return manifest
