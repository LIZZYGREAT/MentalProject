"""Minimal-pair sensitivity and invariant spillover checks."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Mapping

from .common import AnnotationRow


@dataclass(frozen=True)
class OrthogonalityResult:
    pair_id: str
    annotator_id: str
    construct: str
    expectation: str
    left_label: Any
    right_label: Any
    status: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def analyze_orthogonality(
    rows: Iterable[AnnotationRow], pair_designs: Iterable[Mapping[str, Any]]
) -> list[OrthogonalityResult]:
    lookup: dict[tuple[str, str, str], list[Any]] = defaultdict(list)
    annotators: set[str] = set()
    for row in rows:
        lookup[(row.scenario_id, row.annotator_id, row.variable)].append(row.label)
        annotators.add(row.annotator_id)

    results: list[OrthogonalityResult] = []
    for pair in pair_designs:
        left_id, right_id = (str(item) for item in pair["scenario_ids"])
        for annotator in sorted(annotators):
            for expectation, constructs in (
                ("SENSITIVE", pair.get("expected_sensitive_constructs", [])),
                ("INVARIANT", pair.get("expected_invariant_constructs", [])),
            ):
                for construct in constructs:
                    left = lookup.get((left_id, annotator, str(construct)), [])
                    right = lookup.get((right_id, annotator, str(construct)), [])
                    if not left or not right:
                        continue
                    left_label = left[0]
                    right_label = right[0]
                    changed = left_label != right_label
                    if expectation == "SENSITIVE":
                        status = "EXPECTED_CHANGE" if changed else "MISSING_EXPECTED_CHANGE"
                    else:
                        status = "POTENTIAL_SPILLOVER" if changed else "INVARIANT_HELD"
                    results.append(
                        OrthogonalityResult(
                            pair_id=str(pair["pair_id"]),
                            annotator_id=annotator,
                            construct=str(construct),
                            expectation=expectation,
                            left_label=left_label,
                            right_label=right_label,
                            status=status,
                        )
                    )
    return results
