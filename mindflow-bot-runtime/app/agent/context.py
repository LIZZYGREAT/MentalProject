"""Trusted context constructed only by the backend worker."""

from dataclasses import dataclass
from typing import Literal
import uuid


CalendarMutationPolicy = Literal[
    "read_only",
    "calendar_direct_user_request",
    "course_schedule_strict_only",
    "normal",
]


@dataclass(frozen=True)
class AgentContext:
    participant_id: uuid.UUID
    participant_code: str
    open_id: str
    chat_id: str
    message_id: str
    agent_run_id: uuid.UUID
    calendar_mutation_policy: CalendarMutationPolicy = "normal"

    @property
    def calendar_mutation_allowed(self) -> bool:
        """Compatibility view for callers that only need the hard gate."""

        return self.calendar_mutation_policy in {
            "normal",
            "calendar_direct_user_request",
        }
