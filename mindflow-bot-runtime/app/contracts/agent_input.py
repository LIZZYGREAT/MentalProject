"""Backend-owned input contract for text and multimodal Agent turns."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Mapping


@dataclass(frozen=True)
class AgentImageAttachment:
    """A validated image resource bound to the current backend turn."""

    source_message_id: str
    mime_type: str
    data: bytes | None = None
    image_key: str | None = None


@dataclass(frozen=True)
class AgentTurnInput:
    """One natural user turn, independent of the model transport used."""

    text: str
    images: tuple[AgentImageAttachment, ...] = ()
    trusted_image_context: Mapping[str, Any] | None = None
    # Backend-derived context, never a permission. The stage comes from the
    # first real usage time (FeishuBinding.bound_at), not Participant.created_at.
    participant_stage: Literal["day1", "week1", "active"] | None = None
    # These three domains stay separate. They are backend context, never
    # instructions, permissions, or a durable copy of research state.
    participant_memory: tuple[Mapping[str, Any], ...] = ()
    interaction_preferences: Mapping[str, Any] | None = None
    psychological_context: Mapping[str, Any] | None = None
    # Backend-authoritative ingress timestamp for this turn. It is context,
    # never user input, a model assertion, or a permission.
    reference_time_utc: datetime | None = None

    def __str__(self) -> str:
        """Preserve text behavior at legacy adapter/test-double boundaries."""

        return self.text

    @property
    def conversation_text(self) -> str:
        """Safe text stored in conversation history (never image bytes/OCR)."""

        text = self.text.strip()
        return f"[图片] {text}".strip() if self.images or self.trusted_image_context else text


def ensure_agent_turn_input(value: AgentTurnInput | str) -> AgentTurnInput:
    """Compatibility boundary for existing text-only callers and tests."""

    return value if isinstance(value, AgentTurnInput) else AgentTurnInput(text=str(value))
