"""Command line entry point for the Stage 1 offline research package."""

from __future__ import annotations

import argparse
from pathlib import Path
import tempfile
from typing import Sequence

from .validation import Validator


def _build_corpus_command(args: argparse.Namespace) -> int:
    from .corpus import write_calibration

    if args.corpus_set != "calibration":
        raise ValueError(f"unsupported corpus set: {args.corpus_set}")
    counts = write_calibration(args.output_root)
    print(
        "PASS: wrote calibration corpus "
        f"({counts['scenarios']} scenarios, {counts['pairs']} pairs, {counts['anchors']} anchors)"
    )
    return 0


def _build_assignments_command(args: argparse.Namespace) -> int:
    from .assignments import build_assignments

    if args.round_name != "calibration":
        raise ValueError("validation assignments require the formal corpus from Part 8")
    root = Path(__file__).resolve().parent
    summary = build_assignments(
        args.scenarios or root / "scenarios" / "calibration.jsonl",
        args.pair_design or root / "hidden" / "pair_design.jsonl",
        args.output_dir or root / "assignments" / "round_calibration",
        annotation_round="CALIBRATION",
        manual_version=args.manual_version,
        scenario_version=args.scenario_version,
        seed=args.seed,
    )
    counts = {
        annotator: info["selected_scenarios"]
        for annotator, info in summary["annotators"].items()
    }
    print(f"PASS: built calibration assignments {counts}")
    return 0


def _analysis_command(args: argparse.Namespace) -> int:
    from .analysis.pipeline import run_analysis

    root = Path(__file__).resolve().parent
    round_name = args.round_name
    if round_name == "calibration":
        scenarios = root / "scenarios" / "calibration.jsonl"
        annotations = root / "annotations" / "calibration"
        output = root / "analysis" / "outputs" / "calibration"
    else:
        scenarios = [root / "scenarios" / "main.jsonl", root / "scenarios" / "edge.jsonl"]
        annotations = root / "annotations" / round_name
        output = root / "analysis" / "outputs" / round_name
    only = None if args.analysis_kind == "all" else {args.analysis_kind}
    try:
        summary = run_analysis(
            annotations_dir=args.annotations_dir or annotations,
            scenarios_path=args.scenarios or scenarios,
            coverage_path=args.coverage or root / "hidden" / "coverage_tags.jsonl",
            pairs_path=args.pair_design or root / "hidden" / "pair_design.jsonl",
            output_dir=args.output_dir or output,
            only=only,
            quality_thresholds_path=root / "settings" / "quality_thresholds_v1.json",
            repository_root=root.parents[1],
        )
    except ValueError as exc:
        print(f"FAIL: {exc}")
        return 2
    print(f"PASS: analysis complete {summary}")
    return 0


def _manual_gate_command(args: argparse.Namespace) -> int:
    from .gates import evaluate_manual_ready, write_gate_result

    root = Path(__file__).resolve().parent
    result = evaluate_manual_ready(
        manual_path=args.manual or root / "manuals" / "coding_manual_v1.0.md",
        scenarios_path=args.scenarios or root / "scenarios" / "calibration.jsonl",
        coverage_path=args.coverage or root / "hidden" / "coverage_tags.jsonl",
        annotations_dir=args.annotations_dir or root / "annotations" / "calibration",
        analysis_dir=args.analysis_dir or root / "analysis" / "outputs" / "calibration",
        revision_log_path=args.revision_log or root / "adjudication" / "representation_revision_log.jsonl",
        assignments_root=root / "assignments" / "round_calibration",
        pair_design_path=root / "hidden" / "pair_design.jsonl",
        quality_thresholds_path=root / "settings" / "quality_thresholds_v1.json",
    )
    output = args.output or root / "manifests" / "gate_a_manual_ready.json"
    write_gate_result(output, result)
    print(f"{result.status}: MANUAL_READY ({len(result.blocking_reasons)} blocker(s)); result={output}")
    for reason in result.blocking_reasons:
        print(f"- {reason}")
    return 0 if result.status == "PASS" else 2


def _semantic_gate_command(args: argparse.Namespace) -> int:
    from .gates import evaluate_semantic_reliability, write_gate_result

    root = Path(__file__).resolve().parent
    result = evaluate_semantic_reliability(
        main_scenarios_path=args.main_scenarios or root / "scenarios" / "main.jsonl",
        edge_scenarios_path=args.edge_scenarios or root / "scenarios" / "edge.jsonl",
        annotations_dir=args.annotations_dir or root / "annotations" / "validation",
        analysis_dir=args.analysis_dir or root / "analysis" / "outputs" / "validation",
        quality_thresholds_path=args.quality_thresholds,
        assignments_root=root / "assignments" / "round_validation",
        coverage_path=root / "hidden" / "coverage_tags.jsonl",
        pair_design_path=root / "hidden" / "pair_design.jsonl",
    )
    output = args.output or root / "manifests" / "gate_b_semantic_reliability.json"
    write_gate_result(output, result)
    print(f"{result.status}: SEMANTIC_RELIABILITY ({len(result.blocking_reasons)} blocker(s)); result={output}")
    for reason in result.blocking_reasons:
        print(f"- {reason}")
    return 0 if result.status == "PASS" else 2


def _freeze_command(args: argparse.Namespace) -> int:
    from .freeze import FreezeBlockedError, freeze_representation

    root = Path(__file__).resolve().parent
    repository_root = root.parents[1]
    try:
        manifest = freeze_representation(
            repository_root=repository_root,
            gate_a_path=args.gate_a or root / "manifests" / "gate_a_manual_ready.json",
            gate_b_path=args.gate_b or root / "manifests" / "gate_b_semantic_reliability.json",
            manual_path=args.manual or root / "manuals" / "coding_manual_v1.0.md",
            main_scenarios_path=args.main_scenarios or root / "scenarios" / "main.jsonl",
            edge_scenarios_path=args.edge_scenarios or root / "scenarios" / "edge.jsonl",
            gold_path=args.gold or root / "adjudication" / "gold.jsonl",
            revision_log_path=args.revision_log or root / "adjudication" / "representation_revision_log.jsonl",
            field_metrics_path=args.field_metrics or root / "analysis" / "outputs" / "validation" / "field_metrics.csv",
            critical_violation_report_path=args.critical_violations or root / "analysis" / "outputs" / "validation" / "critical_violations.jsonl",
            output_path=args.output or root / "manifests" / "representation_semantics_v1.0.json",
            gate_a_analysis_manifest_path=args.gate_a_analysis_manifest or root / "analysis" / "outputs" / "calibration" / "analysis_manifest.json",
            analysis_manifest_path=args.analysis_manifest or root / "analysis" / "outputs" / "validation" / "analysis_manifest.json",
            quality_thresholds_path=args.quality_thresholds or root / "settings" / "quality_thresholds_v1.json",
            schema_dir=root / "schemas",
            representation_version=args.representation_version,
            manual_version=args.manual_version,
            scenario_version=args.scenario_version,
            gold_version=args.gold_version,
        )
    except (FreezeBlockedError, OSError, ValueError) as exc:
        print(f"FAIL: {exc}")
        return 2
    print(f"PASS: froze Representation Semantics {manifest['representation_version']}")
    return 0


def _reference_set_command(args: argparse.Namespace) -> int:
    from .analysis.common import load_annotation_documents
    from .loader import load_jsonl
    from .reference_set import build_reference_set, load_adjudications, write_reference_set

    try:
        scenarios = load_jsonl(args.scenarios)
        scenario_index = {str(row["scenario_id"]): row for row in scenarios}
        documents = load_annotation_documents(
            args.annotations_dir,
            validate=True,
            scenarios=scenario_index,
            require_scenario_context=True,
        )
        records = build_reference_set(
            annotation_documents=documents,
            scenarios=scenarios,
            adjudications=load_adjudications(args.adjudications),
        )
        count = write_reference_set(args.output, records)
    except (OSError, ValueError) as exc:
        print(f"FAIL: {exc}")
        return 2
    print(f"PASS: built complete Reference Set ({count} target×variable records)")
    return 0


def _export_ai_packets_command(args: argparse.Namespace) -> int:
    from .ai_runner.packets import export_ai_packets

    root = Path(__file__).resolve().parent
    if args.round_name != "calibration":
        print("FAIL: formal packet export is unavailable before Gate A")
        return 2
    slug = args.annotator.lower().replace("-", "_")
    assignment_root = root / "assignments" / "round_calibration" / slug
    modules = (args.module,) if args.module else ("A", "B", "C")
    summaries = []
    for module in modules:
        output_dir = (
            args.output_dir / f"module_{module.lower()}"
            if args.output_dir and len(modules) > 1
            else args.output_dir
            or root / "ai_runner" / "packets" / "round_calibration" / slug / f"module_{module.lower()}"
        )
        summaries.append(
            export_ai_packets(
                annotator_id=args.annotator,
                annotation_round="CALIBRATION",
                module=module,
                assignment_path=assignment_root / f"module_{module.lower()}.jsonl",
                assignment_manifest_path=assignment_root / "manifest.json",
                manual_path=root / "manuals" / "coding_manual_v0.1.md",
                output_schema_path=root / "schemas" / "ai_output.schema.json",
                output_dir=output_dir,
            )
        )
    print(
        "PASS: exported AI packets "
        + ", ".join(
            f"Module {summary['annotation_module']}={summary['scenario_count']}"
            for summary in summaries
        )
    )
    return 0


def _import_ai_output_command(args: argparse.Namespace) -> int:
    from .ai_runner.importer import import_ai_output

    root = Path(__file__).resolve().parent
    if args.round_name != "calibration":
        print("FAIL: formal annotation import is unavailable before Gate A")
        return 2
    slug = args.annotator.lower().replace("-", "_")
    assignment_root = root / "assignments" / "round_calibration" / slug
    output_dir = (
        args.output_dir
        or root / "annotations" / "drafts" / slug / "calibration" / f"module_{args.module.lower()}"
    )
    try:
        path = import_ai_output(
            annotator_id=args.annotator,
            annotation_round="CALIBRATION",
            module=args.module,
            scenario_id=args.scenario_id,
            assignment_path=assignment_root / f"module_{args.module.lower()}.jsonl",
            assignment_manifest_path=assignment_root / "manifest.json",
            raw_output_path=args.input,
            raw_output_schema_path=root / "schemas" / "ai_output.schema.json",
            output_dir=output_dir,
            provider=args.provider,
            model=args.model,
            temperature=args.temperature,
            seed=args.seed,
            request_id=args.request_id,
            attempt=args.attempt,
        )
    except ValueError as exc:
        print(f"FAIL: {exc}")
        return 2
    print(f"PASS: imported validated AI annotation draft {path}")
    return 0


def _export_annotations_command(args: argparse.Namespace) -> int:
    from .drafts import export_annotations

    root = Path(__file__).resolve().parent
    if args.round_name != "calibration":
        print("FAIL: formal annotation export is unavailable before Gate A")
        return 2
    slug = args.annotator.lower().replace("-", "_")
    assignment_root = root / "assignments" / "round_calibration" / slug
    draft_root = root / "annotations" / "drafts" / slug / "calibration"
    output_root = args.output_dir or root / "annotations" / "calibration" / slug
    modules = (args.module,) if args.module else ("A", "B", "C")
    exported: list[str] = []
    try:
        for module in modules:
            count = export_annotations(
                annotator_id=args.annotator,
                module=module,
                assignment_path=assignment_root / f"module_{module.lower()}.jsonl",
                draft_dir=draft_root / f"module_{module.lower()}",
                output_path=output_root / f"module_{module.lower()}.jsonl",
            )
            exported.append(f"Module {module}={count}")
    except (OSError, ValueError) as exc:
        print(f"FAIL: {exc}")
        return 2
    print("PASS: exported validated annotation documents " + ", ".join(exported))
    return 0


def _determinism_command(_: argparse.Namespace) -> int:
    from .determinism import (
        ASSIGNMENT_ARTIFACTS,
        CORPUS_ARTIFACTS,
        byte_mismatches,
        regenerate_calibration,
    )

    root = Path(__file__).resolve().parent
    with tempfile.TemporaryDirectory(prefix="mindflow-stage1-") as temporary:
        generated = Path(temporary)
        regenerate_calibration(generated)
        mismatches = byte_mismatches(
            root, generated, (*CORPUS_ARTIFACTS, *ASSIGNMENT_ARTIFACTS)
        )
    if mismatches:
        print("FAIL: deterministic artifacts differ from the committed files")
        for mismatch in mismatches:
            print(f"- {mismatch}")
        return 1
    print(
        "PASS: committed calibration corpus and seed-12001 assignments "
        "match a clean byte-for-byte regeneration"
    )
    return 0


def _add_analysis_parser(
    subparsers: argparse._SubParsersAction,
    name: str,
    help_text: str,
    analysis_kind: str,
) -> None:
    command = subparsers.add_parser(name, help=help_text)
    command.add_argument("--round", dest="round_name", choices=("calibration", "validation"), required=True)
    command.add_argument("--annotations-dir", type=Path)
    command.add_argument("--scenarios", type=Path)
    command.add_argument("--coverage", type=Path)
    command.add_argument("--pair-design", type=Path)
    command.add_argument("--output-dir", type=Path)
    command.set_defaults(handler=_analysis_command, analysis_kind=analysis_kind)


def _validation_command(args: argparse.Namespace) -> int:
    result = Validator(args.schema_dir).validate_paths(args.paths, args.artifact_type)
    for issue in result.issues:
        print(issue)
    if result.ok:
        print(f"PASS: validated {result.checked} {args.artifact_type} artifact(s)")
        return 0
    print(f"FAIL: {len(result.issues)} issue(s) across {result.checked} artifact(s)")
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MindFlow Stage 1 scenario annotation tooling")
    parser.add_argument(
        "--schema-dir",
        type=Path,
        default=None,
        help="override the packaged JSON Schema directory",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="validate JSON/JSONL artifacts")
    validate.add_argument(
        "artifact_type",
        choices=(
            "scenario",
            "event-annotation",
            "appraisal-annotation",
            "bot-annotation",
            "pair-design",
            "anchor-reference",
            "coverage-tags",
            "assignment-manifest",
            "revision-log",
            "gate-result",
            "adjudication",
            "freeze-manifest",
            "reference-record",
        ),
    )
    validate.add_argument("paths", nargs="+", type=Path)
    validate.set_defaults(handler=_validation_command)

    validate_scenarios = subparsers.add_parser(
        "validate-scenarios", help="validate annotator-visible scenario files"
    )
    validate_scenarios.add_argument("paths", nargs="+", type=Path)
    validate_scenarios.set_defaults(handler=_validation_command, artifact_type="scenario")

    validate_annotations = subparsers.add_parser(
        "validate-annotations", help="validate module-specific annotation files"
    )
    validate_annotations.add_argument(
        "module", choices=("A", "B", "C"), help="annotation module"
    )
    validate_annotations.add_argument(
        "--scenarios",
        type=Path,
        required=True,
        help="annotator-visible scenario or assignment JSONL used for evidence validation",
    )
    validate_annotations.add_argument("paths", nargs="+", type=Path)
    validate_annotations.set_defaults(handler=_validate_annotations)

    build_corpus = subparsers.add_parser(
        "build-corpus", help="write a deterministic visible/hidden scenario corpus"
    )
    build_corpus.add_argument("--set", dest="corpus_set", choices=("calibration",), required=True)
    build_corpus.add_argument(
        "--output-root", type=Path, default=Path(__file__).resolve().parent
    )
    build_corpus.set_defaults(handler=_build_corpus_command)

    assignments = subparsers.add_parser(
        "build-assignments", help="build blind, randomized, counterbalanced assignment inputs"
    )
    assignments.add_argument("--round", dest="round_name", choices=("calibration",), required=True)
    assignments.add_argument("--manual-version", default="0.1")
    assignments.add_argument("--scenario-version", default="0.1")
    assignments.add_argument("--seed", type=int, default=12001)
    assignments.add_argument("--scenarios", type=Path)
    assignments.add_argument("--pair-design", type=Path)
    assignments.add_argument("--output-dir", type=Path)
    assignments.set_defaults(handler=_build_assignments_command)

    _add_analysis_parser(subparsers, "analyze", "run every Stage 1 field-level analysis", "all")
    _add_analysis_parser(subparsers, "analyze-agreement", "write agreement and confusion outputs", "agreement")
    _add_analysis_parser(subparsers, "analyze-critical-violations", "write critical-boundary audits", "violations")
    _add_analysis_parser(subparsers, "analyze-orthogonality", "write minimal-pair checks", "orthogonality")
    _add_analysis_parser(subparsers, "build-disagreement-queue", "build the adjudication queue", "disagreement")
    _add_analysis_parser(subparsers, "build-report", "write the construct-level report", "report")

    gate = subparsers.add_parser("evaluate-manual-gate", help="evaluate Gate A without fabricating evidence")
    gate.add_argument("--manual", type=Path)
    gate.add_argument("--scenarios", type=Path)
    gate.add_argument("--coverage", type=Path)
    gate.add_argument("--annotations-dir", type=Path)
    gate.add_argument("--analysis-dir", type=Path)
    gate.add_argument("--revision-log", type=Path)
    gate.add_argument("--output", type=Path)
    gate.set_defaults(handler=_manual_gate_command)

    semantic_gate = subparsers.add_parser(
        "evaluate-semantic-gate", help="evaluate Gate B after blind validation"
    )
    semantic_gate.add_argument("--main-scenarios", type=Path)
    semantic_gate.add_argument("--edge-scenarios", type=Path)
    semantic_gate.add_argument("--annotations-dir", type=Path)
    semantic_gate.add_argument("--analysis-dir", type=Path)
    semantic_gate.add_argument("--quality-thresholds", type=Path)
    semantic_gate.add_argument("--output", type=Path)
    semantic_gate.set_defaults(handler=_semantic_gate_command)

    freeze = subparsers.add_parser(
        "freeze-representation", help="create the immutable v1.0 manifest after all gates pass"
    )
    freeze.add_argument("--gate-a", type=Path)
    freeze.add_argument("--gate-b", type=Path)
    freeze.add_argument("--manual", type=Path)
    freeze.add_argument("--main-scenarios", type=Path)
    freeze.add_argument("--edge-scenarios", type=Path)
    freeze.add_argument("--gold", type=Path)
    freeze.add_argument("--revision-log", type=Path)
    freeze.add_argument("--field-metrics", type=Path)
    freeze.add_argument("--critical-violations", type=Path)
    freeze.add_argument("--output", type=Path)
    freeze.add_argument("--gate-a-analysis-manifest", type=Path)
    freeze.add_argument("--analysis-manifest", type=Path)
    freeze.add_argument("--quality-thresholds", type=Path)
    freeze.add_argument("--representation-version", default="1.0")
    freeze.add_argument("--manual-version", default="1.0")
    freeze.add_argument("--scenario-version", default="1.0")
    freeze.add_argument("--gold-version", default="1.0")
    freeze.set_defaults(handler=_freeze_command)

    reference = subparsers.add_parser(
        "build-reference-set",
        help="combine unanimous annotations and explicit adjudications into a complete Reference Set",
    )
    reference.add_argument("--annotations-dir", type=Path, required=True)
    reference.add_argument("--scenarios", type=Path, required=True)
    reference.add_argument("--adjudications", type=Path, required=True)
    reference.add_argument("--output", type=Path, required=True)
    reference.set_defaults(handler=_reference_set_command)

    export_packets = subparsers.add_parser(
        "export-ai-packets", help="export provider-neutral one-scenario prompt packets"
    )
    export_packets.add_argument("--round", dest="round_name", choices=("calibration",), required=True)
    export_packets.add_argument("--annotator", choices=("AI-A", "AI-B", "AI-C"), required=True)
    export_packets.add_argument("--module", choices=("A", "B", "C"))
    export_packets.add_argument("--output-dir", type=Path)
    export_packets.set_defaults(handler=_export_ai_packets_command)

    import_output = subparsers.add_parser(
        "import-ai-output", help="validate model fields and create a system-owned annotation draft"
    )
    import_output.add_argument("--round", dest="round_name", choices=("calibration",), required=True)
    import_output.add_argument("--annotator", choices=("AI-A", "AI-B", "AI-C"), required=True)
    import_output.add_argument("--module", choices=("A", "B", "C"), required=True)
    import_output.add_argument("--scenario-id", required=True)
    import_output.add_argument("--input", type=Path, required=True)
    import_output.add_argument("--output-dir", type=Path)
    import_output.add_argument("--provider", required=True)
    import_output.add_argument("--model", required=True)
    import_output.add_argument("--temperature", type=float, default=0.0)
    import_output.add_argument("--seed", type=int)
    import_output.add_argument("--request-id", required=True)
    import_output.add_argument("--attempt", type=int, default=1)
    import_output.set_defaults(handler=_import_ai_output_command)

    export_annotations_parser = subparsers.add_parser(
        "export-annotations",
        help="merge complete per-scenario drafts into validated annotation JSONL",
    )
    export_annotations_parser.add_argument(
        "--round", dest="round_name", choices=("calibration",), required=True
    )
    export_annotations_parser.add_argument(
        "--annotator", choices=("AI-A", "AI-B", "AI-C", "Human"), required=True
    )
    export_annotations_parser.add_argument("--module", choices=("A", "B", "C"))
    export_annotations_parser.add_argument("--output-dir", type=Path)
    export_annotations_parser.set_defaults(handler=_export_annotations_command)

    deterministic = subparsers.add_parser(
        "check-deterministic-artifacts",
        help="regenerate calibration corpus and assignments and compare committed bytes",
    )
    deterministic.set_defaults(handler=_determinism_command)
    return parser


def _validate_annotations(args: argparse.Namespace) -> int:
    from .loader import load_jsonl

    args.artifact_type = {
        "A": "event-annotation",
        "B": "appraisal-annotation",
        "C": "bot-annotation",
    }[args.module]
    scenarios = {str(row["scenario_id"]): row for row in load_jsonl(args.scenarios)}
    result = Validator(args.schema_dir).validate_paths(
        args.paths,
        args.artifact_type,
        scenarios=scenarios,
        require_scenario_context=True,
    )
    for issue in result.issues:
        print(issue)
    if result.ok:
        print(
            f"PASS: validated {result.checked} {args.artifact_type} artifact(s) "
            "with visible evidence integrity"
        )
        return 0
    print(f"FAIL: {len(result.issues)} issue(s) across {result.checked} artifact(s)")
    return 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
