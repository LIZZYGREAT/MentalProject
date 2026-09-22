"""Pairwise label confusion matrices."""

from __future__ import annotations

from collections import Counter, defaultdict
from itertools import combinations
from typing import Any, Iterable

from .common import AnnotationRow


def confusion_matrices(rows: Iterable[AnnotationRow]) -> dict[str, dict[str, int]]:
    units: dict[tuple[str, str, str, str], list[Any]] = defaultdict(list)
    for row in rows:
        units[(row.module, row.variable, row.scenario_id, row.target_ref)].append(row.label)
    matrices: dict[str, Counter[str]] = defaultdict(Counter)
    for (module, variable, _, _), labels in units.items():
        for left, right in combinations(labels, 2):
            pair = " == ".join(sorted((str(left), str(right))))
            matrices[f"{module}:{variable}"][pair] += 1
    return {field: dict(sorted(counter.items())) for field, counter in sorted(matrices.items())}
