"""Conservative delivery-failure classification for CardAction results."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CardUpdateFailurePolicy:
    retryable: bool
    replacement_allowed: bool
    provider_error_code: int | None
    operation: str


def card_update_failure_policy(exc: BaseException) -> CardUpdateFailurePolicy:
    raw_code: Any = getattr(exc, "code", None)
    try:
        provider_error_code = int(raw_code) if raw_code is not None else None
    except (TypeError, ValueError):
        provider_error_code = None
    return CardUpdateFailurePolicy(
        retryable=bool(getattr(exc, "retryable", False)),
        replacement_allowed=bool(getattr(exc, "replacement_allowed", False)),
        provider_error_code=provider_error_code,
        operation=str(getattr(exc, "operation", "unknown") or "unknown")[:64],
    )
