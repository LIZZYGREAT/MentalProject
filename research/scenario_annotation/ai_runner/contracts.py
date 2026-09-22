"""Optional provider adapter contracts; Stage 1 does not depend on an adapter."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Protocol


class RetryReason(str, Enum):
    NETWORK_FAILURE = "NETWORK_FAILURE"
    PROVIDER_TIMEOUT = "PROVIDER_TIMEOUT"
    INVALID_JSON = "INVALID_JSON"
    SCHEMA_FAILURE = "SCHEMA_FAILURE"


FORBIDDEN_RETRY_FEEDBACK = frozenset(
    {"gold", "other_annotator", "human_disagreement", "majority_label", "analysis_report"}
)


@dataclass(frozen=True)
class AIRequestMetadata:
    provider: str
    model: str
    temperature: float
    seed: int | None
    request_id: str
    attempt: int


class AIAnnotatorPort(Protocol):
    """A narrow adapter boundary for a future optional API runner."""

    def annotate(
        self,
        packet: Mapping[str, Any],
        metadata: AIRequestMetadata,
    ) -> Mapping[str, Any]:
        """Return only the model-controlled output contract."""


def retry_allowed(reason: RetryReason, feedback_fields: set[str] | None = None) -> bool:
    return not (FORBIDDEN_RETRY_FEEDBACK & (feedback_fields or set())) and reason in set(RetryReason)
