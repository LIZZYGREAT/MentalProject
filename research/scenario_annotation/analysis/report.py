"""Human-readable construct and disagreement reports."""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Iterable, Mapping

from .agreement import FieldMetric
from .critical_violations import CriticalViolation
from .disagreement import DisagreementItem
from .orthogonality import OrthogonalityResult, orthogonality_summary


def build_disagreement_report(items: Iterable[DisagreementItem]) -> str:
    by_field: dict[str, list[DisagreementItem]] = defaultdict(list)
    for item in items:
        by_field[item.variable].append(item)
    lines = ["# Disagreement Report", "", "Disagreements are clustered by field; no majority label is treated as Gold.", ""]
    if not by_field:
        lines.extend(["No disagreements were found in the supplied annotation records.", ""])
        return "\n".join(lines)
    for field, field_items in sorted(by_field.items()):
        causes = Counter(item.suspected_cause for item in field_items)
        lines.extend(
            [
                f"## {field}",
                "",
                f"- Total disagreements: {len(field_items)}",
                f"- Major: {sum(item.severity == 'MAJOR' for item in field_items)}",
                f"- Minor: {sum(item.severity == 'MINOR' for item in field_items)}",
                "- Common suspected causes:",
            ]
        )
        for cause, count in causes.most_common():
            lines.append(f"  - {cause}: {count}")
        lines.append("")
    return "\n".join(lines)


def build_construct_report(
    metrics: Iterable[FieldMetric],
    violations: Iterable[CriticalViolation],
    orthogonality: Iterable[OrthogonalityResult],
    disagreements: Iterable[DisagreementItem],
) -> str:
    metrics = list(metrics)
    violations = list(violations)
    orthogonality = list(orthogonality)
    disagreements = list(disagreements)
    orthogonality_coverage = orthogonality_summary(orthogonality)
    lines = [
        "# Scenario Annotation Report",
        "",
        "This report is field-level. It does not use an overall score to freeze the representation layer.",
        "",
        "## Orthogonality Coverage",
        "",
        f"- expected_checks: {orthogonality_coverage['expected_checks']}",
        f"- evaluated_checks: {orthogonality_coverage['evaluated_checks']}",
        f"- missing_checks: {orthogonality_coverage['missing_checks']}",
        "",
    ]
    for metric in metrics:
        related_violations = [item for item in violations if metric.field in item.affected_fields]
        related_ortho = [item for item in orthogonality if item.construct == metric.field]
        related_disagreements = [item for item in disagreements if item.variable == metric.field]
        status = {
            "STRONG_CANDIDATE": "KEEP CANDIDATE",
            "REVIEW_REQUIRED": "REVISE/REVIEW",
            "RECONSIDER": "REVISE/SIMPLIFY/DROP REVIEW",
            "FACT_REVIEW": "FACT DEVIATION REVIEW",
        }.get(metric.freeze_status, "INSUFFICIENT DATA")
        lines.extend(
            [
                f"## Construct: {metric.field}",
                "",
                f"- Status: {status}",
                "- Definition Version: supplied manual version",
                f"- N: {metric.n_valid}",
                f"- Unknown/No-Evidence Rate: {metric.unknown_rate:.3f}",
                f"- Agreement alpha: {_fmt(metric.krippendorff_alpha)} ({metric.alpha_type})",
                f"- Raw agreement: {_fmt(metric.raw_agreement)}",
                f"- Critical Violations: {len(related_violations)}",
                f"- Common Confusions: {len(related_disagreements)} queued disagreements",
                (
                    "- Minimal-Pair Orthogonality: "
                f"{sum(item.outcome == 'POTENTIAL_SPILLOVER' for item in related_ortho)} spillover(s), "
                f"{sum(item.outcome == 'MISSING_EXPECTED_CHANGE' for item in related_ortho)} missing expected change(s), "
                f"{sum(item.status != 'EVALUATED' for item in related_ortho)} unevaluated check(s)"
                ),
                "- Adjudication Summary: pending explicit adjudication; majority vote not used",
                f"- Decision: {status}",
                "- Open Questions: review queued evidence spans and boundary violations before freeze",
                "",
            ]
        )
    return "\n".join(lines)


def _fmt(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.3f}"
