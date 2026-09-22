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


@dataclass(frozen=True)
class PendingCardUpdate:
    message_id: str
    card: dict[str, Any]


@dataclass(frozen=True)
class ReviewCardPolicy:
    self_contained: bool = True
    suppress_companion: bool = True
    fallback_text: str = "确认卡暂时未能发送，本次变更尚未生效，请稍后重试。"


class PresentationOutbox:
    def __init__(self, *, max_cards_per_turn: int = 2):
        self.max_cards_per_turn = max(1, int(max_cards_per_turn))
        self._cards: dict[
            uuid.UUID,
            list[dict[str, Any] | PendingImageCard | PendingCardUpdate],
        ] = {}
        self._review_policies: dict[uuid.UUID, ReviewCardPolicy] = {}

    def stage_card(
        self,
        run_id: uuid.UUID,
        card: dict[str, Any],
        *,
        review_policy: ReviewCardPolicy | None = None,
    ) -> None:
        items = self._cards.setdefault(run_id, [])
        if len(items) >= self.max_cards_per_turn:
            raise ValueError("too many rich replies in one turn")
        items.append(dict(card))
        self._record_review_policy(run_id, review_policy)

    def stage_card_update(
        self,
        run_id: uuid.UUID,
        message_id: str,
        card: dict[str, Any],
        *,
        review_policy: ReviewCardPolicy | None = None,
    ) -> None:
        normalized = str(message_id).strip()
        if not normalized:
            raise ValueError("card update message id is required")
        items = self._cards.setdefault(run_id, [])
        if len(items) >= self.max_cards_per_turn:
            raise ValueError("too many rich replies in one turn")
        items.append(PendingCardUpdate(normalized, dict(card)))
        self._record_review_policy(run_id, review_policy)

    def _record_review_policy(
        self,
        run_id: uuid.UUID,
        policy: ReviewCardPolicy | None,
    ) -> None:
        if policy is None:
            return
        if policy.self_contained and not str(policy.fallback_text).strip():
            raise ValueError("self-contained review card requires fallback text")
        current = self._review_policies.get(run_id)
        if current is None:
            self._review_policies[run_id] = policy
            return
        self._review_policies[run_id] = ReviewCardPolicy(
            self_contained=current.self_contained and policy.self_contained,
            suppress_companion=(
                current.suppress_companion and policy.suppress_companion
            ),
            fallback_text=(
                current.fallback_text
                if current.fallback_text == policy.fallback_text
                else ReviewCardPolicy().fallback_text
            ),
        )

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
    ) -> list[dict[str, Any] | PendingImageCard | PendingCardUpdate]:
        cards, _policy = self.take_delivery(run_id)
        return cards

    def take_delivery(
        self, run_id: uuid.UUID
    ) -> tuple[
        list[dict[str, Any] | PendingImageCard | PendingCardUpdate],
        ReviewCardPolicy | None,
    ]:
        return (
            self._cards.pop(run_id, []),
            self._review_policies.pop(run_id, None),
        )

    def discard(self, run_id: uuid.UUID) -> None:
        self._cards.pop(run_id, None)
        self._review_policies.pop(run_id, None)
