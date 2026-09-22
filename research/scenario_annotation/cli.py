"""Command line entry point for the Stage 1 offline research package."""

from __future__ import annotations

import argparse
from pathlib import Path
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
        scenarios = root / "scenarios" / ("main.jsonl" if round_name == "validation" else "calibration.jsonl")
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
        )
    except ValueError as exc:
        print(f"FAIL: {exc}")
        return 2
    print(f"PASS: analysis complete {summary}")
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
            "adjudication",
            "freeze-manifest",
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
    return parser


def _validate_annotations(args: argparse.Namespace) -> int:
    args.artifact_type = {
        "A": "event-annotation",
        "B": "appraisal-annotation",
        "C": "bot-annotation",
    }[args.module]
    return _validation_command(args)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
