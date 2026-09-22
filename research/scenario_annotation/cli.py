"""Command line entry point for the Stage 1 offline research package."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from .validation import Validator


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
