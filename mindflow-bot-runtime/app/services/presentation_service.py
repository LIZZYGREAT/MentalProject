"""In-memory handoff for reviewed rich replies produced during an Agent turn."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import uuid
from typing import Any


IMAGE_KEY_PLACEHOLDER = "__MINDFLOW_FEISHU_IMAGE_KEY__"


@dataclass(frozen=True)
class PendingImageCard:
    png_bytes: bytes
    card_template: dict[str, Any]

    def materialize(self, image_key: str) -> dict[str, Any]:
        def replace(value: Any) -> Any:
            if isinstance(value, dict):
                return {key: replace(child) for key, child in value.items()}
            if isinstance(value, list):
                return [replace(child) for child in value]
            return image_key if value == IMAGE_KEY_PLACEHOLDER else value

        return replace(deepcopy(self.card_template))


class PresentationOutbox:
    def __init__(self, *, max_cards_per_turn: int = 2):
        self.max_cards_per_turn = max(1, int(max_cards_per_turn))
        self._cards: dict[uuid.UUID, list[dict[str, Any] | PendingImageCard]] = {}

    def stage_card(self, run_id: uuid.UUID, card: dict[str, Any]) -> None:
        items = self._cards.setdefault(run_id, [])
        if len(items) >= self.max_cards_per_turn:
            raise ValueError("too many rich replies in one turn")
        items.append(dict(card))

    def stage_image_card(
        self, run_id: uuid.UUID, png_bytes: bytes, card_template: dict[str, Any]
    ) -> None:
        if not png_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("rich reply image must be PNG bytes")
        items = self._cards.setdefault(run_id, [])
        if len(items) >= self.max_cards_per_turn:
            raise ValueError("too many rich replies in one turn")
        items.append(PendingImageCard(bytes(png_bytes), dict(card_template)))

    def take_cards(
        self, run_id: uuid.UUID
    ) -> list[dict[str, Any] | PendingImageCard]:
        return self._cards.pop(run_id, [])

    def discard(self, run_id: uuid.UUID) -> None:
        self._cards.pop(run_id, None)
