"""Backend validation boundary for Agent-composed care drafts."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Mapping

from app.agent.care_composer import CareDraft
from app.contracts.care_evidence import CareEvidencePacket


CARE_DRAFT_VALIDATOR_VERSION = "care_draft_validator.v1"
_DANGEROUS = re.compile(r"诊断|焦虑症|抑郁症|你一定是|必须马上|证明了你", re.IGNORECASE)


@dataclass(frozen=True)
class CareDraftValidation:
    valid: bool
    draft: CareDraft | None
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "reason": self.reason,
            "draft": self.draft.to_dict() if self.draft else None,
        }


class CareDraftValidator:
    version = CARE_DRAFT_VALIDATOR_VERSION

    def validate(
        self,
        evidence: CareEvidencePacket | Mapping[str, Any],
        draft: CareDraft | Mapping[str, Any],
    ) -> CareDraftValidation:
        packet = evidence if isinstance(evidence, CareEvidencePacket) else CareEvidencePacket.from_dict(evidence)
        candidate = draft if isinstance(draft, CareDraft) else CareDraft.from_payload(draft)
        message = candidate.message.strip()
        allowed_type = str(packet.intervention.get("allowed_type") or "")
        action_minutes = int(packet.intervention.get("action_minutes") or 0)
        if not message:
            return CareDraftValidation(False, None, "empty_message")
        if not 120 <= len(message) <= 360:
            return CareDraftValidation(False, None, "message_length_out_of_range")
        if _DANGEROUS.search(message):
            return CareDraftValidation(False, None, "unsafe_diagnostic_language")
        if allowed_type and candidate.intervention_type != allowed_type:
            return CareDraftValidation(False, None, "intervention_type_changed")
        if action_minutes > 0 and not 1 <= candidate.action_minutes <= action_minutes:
            return CareDraftValidation(False, None, "action_minutes_out_of_range")
        if not set(candidate.selected_fact_ids) <= packet.fact_ids:
            return CareDraftValidation(False, None, "unknown_fact_id")
        if not set(candidate.selected_reason_codes) <= packet.reason_codes:
            return CareDraftValidation(False, None, "unknown_reason_code")
        if len(candidate.selected_reason_codes) > 2:
            return CareDraftValidation(False, None, "too_many_reason_codes")
        return CareDraftValidation(True, candidate, None)
