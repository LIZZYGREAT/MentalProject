"""Backend-owned input contract for text and multimodal Agent turns."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


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

    @property
    def conversation_text(self) -> str:
        """Safe text stored in conversation history (never image bytes/OCR)."""

        text = self.text.strip()
        return f"[图片] {text}".strip() if self.images or self.trusted_image_context else text
