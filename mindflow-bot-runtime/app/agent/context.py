"""Trusted context constructed only by the backend worker."""

from dataclasses import dataclass
import uuid


@dataclass(frozen=True)
class AgentContext:
    participant_id: uuid.UUID
    participant_code: str
    open_id: str
    chat_id: str
    message_id: str
    agent_run_id: uuid.UUID
