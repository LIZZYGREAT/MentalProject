"""Data contracts between the Agent, presentation, and delivery layers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, Literal


ResponseKind = Literal[
    "fixed",
    "conversation",
    "transactional",
    "analysis",
    "rich",
    "error",
]

ActivityKind = Literal[
    "thinking",
    "tool_started",
    "tool_succeeded",
    "tool_failed",
]


@dataclass(frozen=True)
class AgentActivityEvent:
    kind: ActivityKind
    tool_name: str | None = None
    status: str | None = None


AgentActivityCallback = Callable[[AgentActivityEvent], Awaitable[None]]


@dataclass(frozen=True, eq=False)
class RuntimeResponse:
    """Authoritative, post-Safety result from the main Agent."""

    text: str
    safety_locked: bool = False
    response_kind: ResponseKind = "conversation"

    def __str__(self) -> str:
        return self.text

    def __eq__(self, other: object) -> bool:
        if isinstance(other, RuntimeResponse):
            return (
                self.text,
                self.safety_locked,
                self.response_kind,
            ) == (
                other.text,
                other.safety_locked,
                other.response_kind,
            )
        if isinstance(other, str):
            return self.text == other
        return NotImplemented

    def __hash__(self) -> int:
        return hash((self.text, self.safety_locked, self.response_kind))


@dataclass(frozen=True)
class ResponseSegment:
    index: int
    text: str


@dataclass(frozen=True)
class ResponsePlan:
    kind: ResponseKind
    full_text: str
    segments: tuple[ResponseSegment, ...]
    use_cards: bool
    presentation_agent_used: bool = False

