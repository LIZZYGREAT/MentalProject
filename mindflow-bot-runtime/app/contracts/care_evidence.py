"""Versioned, bounded evidence contract for personalized care messages."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


CARE_EVIDENCE_SCHEMA_VERSION = "care_evidence.v1"
CARE_REASON_POLICY_VERSION = "care_reason_policy.v1"


def _copy_mapping(value: Mapping[str, Any] | None) -> dict[str, Any]:
    return dict(value or {})


@dataclass(frozen=True)
class CareEvidencePacket:
    schema_version: str
    source: str
    local_date: str
    risk: dict[str, Any]
    trajectory: dict[str, Any]
    schedule: dict[str, Any]
    event_facts: tuple[dict[str, Any], ...]
    recent_state: dict[str, Any]
    personalization: dict[str, Any]
    care_history: dict[str, Any]
    intervention: dict[str, Any]
    reason_candidates: tuple[dict[str, Any], ...]

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CareEvidencePacket":
        payload = dict(value or {})
        return cls(
            schema_version=str(payload.get("schema_version") or CARE_EVIDENCE_SCHEMA_VERSION),
            source=str(payload.get("source") or "forecast_warning"),
            local_date=str(payload.get("local_date") or ""),
            risk=_copy_mapping(payload.get("risk")),
            trajectory=_copy_mapping(payload.get("trajectory")),
            schedule=_copy_mapping(payload.get("schedule")),
            event_facts=tuple(
                dict(item) for item in list(payload.get("event_facts") or [])[:8]
                if isinstance(item, Mapping)
            ),
            recent_state=_copy_mapping(payload.get("recent_state")),
            personalization=_copy_mapping(payload.get("personalization")),
            care_history=_copy_mapping(payload.get("care_history")),
            intervention=_copy_mapping(payload.get("intervention")),
            reason_candidates=tuple(
                dict(item) for item in list(payload.get("reason_candidates") or [])[:3]
                if isinstance(item, Mapping)
            ),
        )

    @property
    def fact_ids(self) -> set[str]:
        return {
            str(item.get("fact_id"))
            for item in self.event_facts
            if str(item.get("fact_id") or "")
        }

    @property
    def reason_codes(self) -> set[str]:
        return {
            str(item.get("code"))
            for item in self.reason_candidates
            if str(item.get("code") or "")
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "source": self.source,
            "local_date": self.local_date,
            "risk": dict(self.risk),
            "trajectory": dict(self.trajectory),
            "schedule": dict(self.schedule),
            "event_facts": [dict(item) for item in self.event_facts],
            "recent_state": dict(self.recent_state),
            "personalization": dict(self.personalization),
            "care_history": dict(self.care_history),
            "intervention": dict(self.intervention),
            "reason_candidates": [dict(item) for item in self.reason_candidates],
        }
