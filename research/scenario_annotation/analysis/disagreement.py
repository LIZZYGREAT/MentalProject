"""Disagreement severity classification and adjudication queue construction."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from typing import Any, Iterable

from .agreement import ORDINAL_ORDERS
from .common import AnnotationRow


@dataclass(frozen=True)
class DisagreementItem:
    disagreement_id: str
    scenario_id: str
    module: str
    target_ref: str
    variable: str
    severity: str
    labels: dict[str, Any]
    annotation_ids: tuple[str, ...]
    evidence_spans: dict[str, str]
    suspected_cause: str
    status: str = "PENDING_ADJUDICATION"

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["annotation_ids"] = list(self.annotation_ids)
        return value


def _severity(variable: str, labels: list[Any]) -> str:
    order = ORDINAL_ORDERS.get(variable)
    if order and all(label in order for label in labels):
        ranks = [order.index(label) for label in labels]
        return "MAJOR" if max(ranks) - min(ranks) >= 2 else "MINOR"
    if "NO_EVIDENCE" in labels and any(label in {"LOW", "MEDIUM", "HIGH"} for label in labels):
        return "MAJOR"
    return "MAJOR"


def _cause(variable: str, labels: list[Any]) -> str:
    if variable == "U_CONTEXT":
        return "possible U_perc leakage or structural-evidence interpretation"
    if variable == "C_EXEC":
        return "possible objective-difficulty inference"
    if variable == "F_REC":
        return "possible preference/occurrence used as recovery fit"
    if variable == "LIFECYCLE":
        return "possible scheduled/realized confusion"
    if "NO_EVIDENCE" in labels:
        return "evidence-span threshold disagreement"
    return "manual boundary, scenario ambiguity, or annotator error"


def build_disagreement_queue(rows: Iterable[AnnotationRow]) -> list[DisagreementItem]:
    groups: dict[tuple[str, str, str, str], list[AnnotationRow]] = defaultdict(list)
    for row in rows:
        groups[(row.scenario_id, row.module, row.target_ref, row.variable)].append(row)
    queue: list[DisagreementItem] = []
    for (scenario_id, module, target_ref, variable), grouped_rows in sorted(groups.items()):
        labels = {row.annotator_id: row.label for row in grouped_rows}
        if len(labels) < 2 or len({str(label) for label in labels.values()}) <= 1:
            continue
        queue.append(
            DisagreementItem(
                disagreement_id=f"DIS:{scenario_id}:{module}:{target_ref}:{variable}",
                scenario_id=scenario_id,
                module=module,
                target_ref=target_ref,
                variable=variable,
                severity=_severity(variable, list(labels.values())),
                labels=labels,
                annotation_ids=tuple(sorted(row.annotation_id for row in grouped_rows)),
                evidence_spans={
                    row.annotator_id: str(row.record["evidence_span"])
                    for row in grouped_rows
                    if row.record.get("evidence_span")
                },
                suspected_cause=_cause(variable, list(labels.values())),
            )
        )
    return queue
