"""Trusted context constructed only by the backend worker."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
import uuid


CalendarMutationPolicy = Literal[
    "read_only",
    "calendar_create_only",
    "calendar_update_only",
    "calendar_delete_only",
    "course_schedule_strict_only",
    "normal",
]
CalendarMutationOperation = Literal["create", "update", "delete"]
TurnEffectPolicy = Literal[
    "verify_on_demand",
    "read_compute_only",
    "deterministic_backend_action",
]
SourceKind = Literal[
    "text",
    "generic_image",
    "course_schedule_strict",
    "card_action",
]
AuthorizationSemanticRole = Literal["user", "assistant"]

_CALENDAR_MUTATIONS_BY_POLICY: dict[
    CalendarMutationPolicy, frozenset[CalendarMutationOperation]
] = {
    "read_only": frozenset(),
    "calendar_create_only": frozenset({"create"}),
    "calendar_update_only": frozenset({"update"}),
    "calendar_delete_only": frozenset({"delete"}),
    "course_schedule_strict_only": frozenset(),
    "normal": frozenset({"create", "update", "delete"}),
}


@dataclass(frozen=True)
class AuthorizationSemanticTurn:
    """One backend-supplied conversation turn used only for mutation review."""

    role: AuthorizationSemanticRole
    text: str


@dataclass(frozen=True)
class AgentContext:
    participant_id: uuid.UUID
    participant_code: str
    open_id: str
    chat_id: str
    message_id: str
    agent_run_id: uuid.UUID
    calendar_mutation_policy: CalendarMutationPolicy = "normal"
    turn_effect_policy: TurnEffectPolicy = "verify_on_demand"
    user_request_text: str = ""
    received_at_utc: datetime | None = None
    source_kind: SourceKind = "text"
    authorization_semantic_context: tuple[AuthorizationSemanticTurn, ...] = ()
    access_tier: str = "participant"
    scopes: tuple[str, ...] = ()

    @property
    def calendar_mutation_allowed(self) -> bool:
        """Compatibility view for callers that only need the hard gate."""

        return bool(_CALENDAR_MUTATIONS_BY_POLICY[self.calendar_mutation_policy])

    def allows_calendar_mutation(
        self, operation: CalendarMutationOperation
    ) -> bool:
        """Authorize one explicit calendar operation at the backend boundary."""

        return operation in _CALENDAR_MUTATIONS_BY_POLICY[
            self.calendar_mutation_policy
        ]
