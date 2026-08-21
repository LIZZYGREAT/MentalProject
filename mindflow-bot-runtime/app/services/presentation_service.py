"""In-memory handoff for reviewed rich replies produced during an Agent turn."""

from __future__ import annotations

import uuid
from typing import Any


class PresentationOutbox:
    def __init__(self, *, max_cards_per_turn: int = 2):
        self.max_cards_per_turn = max(1, int(max_cards_per_turn))
        self._cards: dict[uuid.UUID, list[dict[str, Any]]] = {}

    def stage_card(self, run_id: uuid.UUID, card: dict[str, Any]) -> None:
        items = self._cards.setdefault(run_id, [])
        if len(items) >= self.max_cards_per_turn:
            raise ValueError("too many rich replies in one turn")
        items.append(dict(card))

    def take_cards(self, run_id: uuid.UUID) -> list[dict[str, Any]]:
        return self._cards.pop(run_id, [])

    def discard(self, run_id: uuid.UUID) -> None:
        self._cards.pop(run_id, None)
