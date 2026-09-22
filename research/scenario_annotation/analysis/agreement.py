"""Field-level agreement metrics, including Krippendorff alpha."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from itertools import combinations
from typing import Any, Callable, Iterable

from .common import AnnotationRow


UNKNOWN_LABELS = {"UNKNOWN", "NO_EVIDENCE", "AMBIGUOUS", "N/A"}

ORDINAL_ORDERS: dict[str, tuple[Any, ...]] = {
    "D_POT": ("LOW", "MEDIUM", "HIGH"),
    "U_CONTEXT": ("LOW", "MEDIUM", "HIGH"),
    "R_POT": ("LOW", "MEDIUM", "HIGH"),
    "DEADLINE_SCARCITY_BAND": ("LOW", "MEDIUM", "HIGH"),
    "M_CONTEXT": ("POOR", "PARTIAL", "GOOD"),
    "D_S": ("ABSENT", "PRESENT", "STRONG"),
    "C_EXEC": ("LOW", "MEDIUM", "HIGH"),
    "IMPORTANCE": ("LOW", "MEDIUM", "HIGH"),
    "C_OUT": ("LOW", "MEDIUM", "HIGH"),
    "U_PERC": ("LOW", "MEDIUM", "HIGH"),
    "F_REC": ("LOW", "MEDIUM", "HIGH"),
    "VALIDATION": (0, 0.5, 1),
    "GUIDANCE": (0, 0.5, 1),
    "RELEVANCE": (0, 0.5, 1),
}

FACT_FIELDS = {
    "SCHEDULED_START",
    "SCHEDULED_END",
    "ACTUAL_START",
    "ACTUAL_END",
    "DEADLINE",
    "PROGRESS",
    "ESTIMATED_TOTAL_EFFORT",
    "REMAINING_EFFORT",
}


@dataclass(frozen=True)
class FieldMetric:
    field: str
    module: str
    n_valid: int
    n_unknown: int
    unknown_rate: float
    raw_agreement: float | None
    krippendorff_alpha: float | None
    alpha_type: str
    major_disagreement_rate: float
    minor_disagreement_rate: float
    human_vs_ai_a: float | None
    human_vs_ai_b: float | None
    human_vs_ai_c: float | None
    mean_absolute_deviation: float | None
    freeze_status: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _distance(variable: str) -> tuple[str, Callable[[Any, Any], float]]:
    if variable in FACT_FIELDS:
        def fact_distance(left: Any, right: Any) -> float:
            if isinstance(left, (int, float)) and isinstance(right, (int, float)):
                return float(abs(left - right))
            return 0.0 if left == right else 1.0
        return "fact", fact_distance
    order = ORDINAL_ORDERS.get(variable)
    if order:
        ranks = {label: index for index, label in enumerate(order)}
        denominator = max(1, len(order) - 1)

        def ordinal_distance(left: Any, right: Any) -> float:
            if left in UNKNOWN_LABELS or right in UNKNOWN_LABELS:
                return 0.0 if left == right else 1.0
            if left not in ranks or right not in ranks:
                return 0.0 if left == right else 1.0
            return ((ranks[left] - ranks[right]) / denominator) ** 2

        return "ordinal", ordinal_distance
    return "nominal", lambda left, right: 0.0 if left == right else 1.0


def krippendorff_alpha(units: Iterable[list[Any]], distance: Callable[[Any, Any], float]) -> float | None:
    unit_pairs: list[tuple[Any, Any]] = []
    pooled: list[Any] = []
    for labels in units:
        present = [label for label in labels if label is not None]
        pooled.extend(present)
        unit_pairs.extend(combinations(present, 2))
    if not unit_pairs or len(pooled) < 2:
        return None
    observed = sum(distance(left, right) for left, right in unit_pairs) / len(unit_pairs)
    expected_pairs = list(combinations(pooled, 2))
    expected = sum(distance(left, right) for left, right in expected_pairs) / len(expected_pairs)
    if expected == 0:
        return 1.0 if observed == 0 else None
    return 1.0 - observed / expected


def _pair_agreement(grouped: dict[tuple[str, str], dict[str, Any]], left: str, right: str) -> float | None:
    pairs = [
        labels[left] == labels[right]
        for labels in grouped.values()
        if left in labels and right in labels
    ]
    return sum(pairs) / len(pairs) if pairs else None


def analyze_agreement(rows: Iterable[AnnotationRow]) -> list[FieldMetric]:
    by_field: dict[tuple[str, str], list[AnnotationRow]] = defaultdict(list)
    for row in rows:
        by_field[(row.module, row.variable)].append(row)

    metrics: list[FieldMetric] = []
    for (module, variable), field_rows in sorted(by_field.items()):
        units: dict[tuple[str, str], dict[str, Any]] = defaultdict(dict)
        for row in field_rows:
            units[(row.scenario_id, row.target_ref)][row.annotator_id] = row.label
        label_lists = [list(labels.values()) for labels in units.values()]
        comparable_pairs = [pair for labels in label_lists for pair in combinations(labels, 2)]
        exact = [left == right for left, right in comparable_pairs]
        raw_agreement = sum(exact) / len(exact) if exact else None
        alpha_type, distance = _distance(variable)
        alpha = None if alpha_type == "fact" else krippendorff_alpha(label_lists, distance)

        major = 0
        minor = 0
        disagreeing_units = 0
        order = ORDINAL_ORDERS.get(variable)
        for labels in label_lists:
            if len(set(map(str, labels))) <= 1:
                continue
            disagreeing_units += 1
            if order and all(label in order for label in labels):
                ranks = [order.index(label) for label in labels]
                if max(ranks) - min(ranks) >= 2:
                    major += 1
                else:
                    minor += 1
            else:
                major += 1

        deviations = [
            abs(float(left) - float(right))
            for left, right in comparable_pairs
            if isinstance(left, (int, float)) and isinstance(right, (int, float))
        ]
        unknown = sum(row.label in UNKNOWN_LABELS for row in field_rows)
        n_valid = len(field_rows)
        if alpha_type == "fact":
            freeze_status = "FACT_REVIEW"
        elif alpha is None:
            freeze_status = "INSUFFICIENT_DATA"
        elif alpha >= 0.80:
            freeze_status = "STRONG_CANDIDATE"
        elif alpha >= 0.67:
            freeze_status = "REVIEW_REQUIRED"
        else:
            freeze_status = "RECONSIDER"
        metrics.append(
            FieldMetric(
                field=variable,
                module=module,
                n_valid=n_valid,
                n_unknown=unknown,
                unknown_rate=unknown / n_valid if n_valid else 0.0,
                raw_agreement=raw_agreement,
                krippendorff_alpha=alpha,
                alpha_type=alpha_type,
                major_disagreement_rate=major / len(units) if units else 0.0,
                minor_disagreement_rate=minor / len(units) if units else 0.0,
                human_vs_ai_a=_pair_agreement(units, "Human", "AI-A"),
                human_vs_ai_b=_pair_agreement(units, "Human", "AI-B"),
                human_vs_ai_c=_pair_agreement(units, "Human", "AI-C"),
                mean_absolute_deviation=(sum(deviations) / len(deviations) if deviations else None),
                freeze_status=freeze_status,
            )
        )
    return metrics
