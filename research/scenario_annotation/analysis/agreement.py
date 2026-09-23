"""Field-level agreement metrics with coincidence-normalized Krippendorff alpha."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
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
    n_units: int
    pairable_units: int
    min_raters_per_unit: int
    max_raters_per_unit: int
    mean_raters_per_unit: float
    rater_count_distribution: dict[int, int]
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


def _ordinal_distance(
    order: tuple[Any, ...], units: list[list[Any]]
) -> tuple[str, Callable[[Any, Any], float]]:
    ranks = {label: index for index, label in enumerate(order)}
    pairable = [[label for label in unit if label is not None] for unit in units]
    pairable = [unit for unit in pairable if len(unit) >= 2]
    frequencies = Counter(label for unit in pairable for label in unit if label in ranks)

    def distance(left: Any, right: Any) -> float:
        if left == right:
            return 0.0
        if left not in ranks or right not in ranks:
            return 1.0
        low, high = sorted((ranks[left], ranks[right]))
        between = sum(frequencies[order[index]] for index in range(low, high + 1))
        endpoint_half = (frequencies[left] + frequencies[right]) / 2
        return float((between - endpoint_half) ** 2)

    values = [label for unit in pairable for label in unit]
    all_ordered = all(label in ranks for label in values)
    return ("ordinal" if all_ordered else "ordinal_mixed"), distance


def _distance(
    variable: str, units: list[list[Any]] | None = None
) -> tuple[str, Callable[[Any, Any], float]]:
    if variable in FACT_FIELDS:
        def fact_distance(left: Any, right: Any) -> float:
            numeric_left = isinstance(left, (int, float)) and not isinstance(left, bool)
            numeric_right = isinstance(right, (int, float)) and not isinstance(right, bool)
            if numeric_left and numeric_right:
                return float(abs(left - right))
            return 0.0 if left == right else 1.0

        return "fact", fact_distance
    order = ORDINAL_ORDERS.get(variable)
    if order:
        return _ordinal_distance(order, units or [])
    return "nominal", lambda left, right: 0.0 if left == right else 1.0


def krippendorff_alpha(
    units: Iterable[list[Any]], distance: Callable[[Any, Any], float]
) -> float | None:
    """Compute alpha from the standard coincidence matrix.

    Each unit with ``m`` observed ratings contributes ordered coincidences with
    weight ``1 / (m - 1)``. Units with fewer than two observed ratings do not
    contribute. This keeps units with different rater counts correctly
    normalized and supports missing ratings without listwise deletion.
    """
    pairable: list[list[Any]] = []
    for unit in units:
        present = [label for label in unit if label is not None]
        if len(present) >= 2:
            pairable.append(present)
    if not pairable:
        return None

    coincidence: Counter[tuple[Any, Any]] = Counter()
    for unit in pairable:
        counts = Counter(unit)
        denominator = len(unit) - 1
        for left, left_count in counts.items():
            for right, right_count in counts.items():
                distinct_pairs = left_count * (right_count - int(left == right))
                if distinct_pairs:
                    coincidence[(left, right)] += distinct_pairs / denominator

    marginals: Counter[Any] = Counter()
    for (left, _), count in coincidence.items():
        marginals[left] += count
    n = sum(marginals.values())
    if n < 2:
        return None

    observed_disagreement = sum(
        weight * distance(left, right)
        for (left, right), weight in coincidence.items()
    ) / n
    expected_disagreement = sum(
        left_count * right_count * distance(left, right)
        for left, left_count in marginals.items()
        for right, right_count in marginals.items()
    ) / (n * (n - 1))
    if expected_disagreement == 0:
        return 1.0 if observed_disagreement == 0 else None
    return 1.0 - observed_disagreement / expected_disagreement


def _pair_agreement(
    grouped: dict[tuple[str, str], dict[str, Any]], left: str, right: str
) -> float | None:
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
        unit_labels = [list(labels.values()) for labels in units.values()]
        rater_counts = [sum(value is not None for value in labels) for labels in unit_labels]
        pairable = sum(count >= 2 for count in rater_counts)
        rater_distribution = dict(sorted(Counter(rater_counts).items()))
        comparable_pairs = [
            (left, right)
            for labels in unit_labels
            for index, left in enumerate(labels)
            for right in labels[index + 1:]
        ]
        exact = [left == right for left, right in comparable_pairs]
        raw_agreement = sum(exact) / len(exact) if exact else None
        alpha_type, distance = _distance(variable, unit_labels)
        alpha = None if alpha_type == "fact" else krippendorff_alpha(unit_labels, distance)

        major = 0
        minor = 0
        order = ORDINAL_ORDERS.get(variable)
        for labels in unit_labels:
            present = [label for label in labels if label is not None]
            if len(set(map(str, present))) <= 1:
                continue
            if order and all(label in order for label in present):
                ranks = [order.index(label) for label in present]
                if max(ranks) - min(ranks) >= 2:
                    major += 1
                else:
                    minor += 1
            else:
                major += 1

        deviations = [
            abs(float(left) - float(right))
            for left, right in comparable_pairs
            if isinstance(left, (int, float)) and not isinstance(left, bool)
            and isinstance(right, (int, float)) and not isinstance(right, bool)
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
                n_units=len(unit_labels),
                pairable_units=pairable,
                min_raters_per_unit=min(rater_counts, default=0),
                max_raters_per_unit=max(rater_counts, default=0),
                mean_raters_per_unit=(
                    sum(rater_counts) / len(rater_counts) if rater_counts else 0.0
                ),
                rater_count_distribution=rater_distribution,
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
