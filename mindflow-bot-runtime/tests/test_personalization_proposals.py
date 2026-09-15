import asyncio
import json
from types import SimpleNamespace
import uuid

from app.agent.context import AgentContext
from app.agent.tool_registry import ToolRegistry
from app.card_actions.registry import card_action_spec
from app.repositories import ObservationRepository
from app.repositories_memory import ParticipantMemoryRepository
from app.repositories_personalization_proposal import (
    PersonalizationProposalRepository,
)
from app.repositories_preferences import InteractionPreferenceRepository
from app.repositories_support_preferences import SupportPreferenceRepository
from app.services.card_action_service import CardActionService
from app.services.interaction_preference_service import InteractionPreferenceService
from app.services.memory_service import MemoryService
from app.services.personalization_proposal_service import (
    PersonalizationProposalService,
)
from app.services.presentation_service import PresentationOutbox
from app.tools.memory import MemoryTools
from app.tools.preferences import InteractionPreferenceTools
from tests.helpers import memory_database, participant


class _FailIfCalledVerifier:
    def __init__(self):
        self.calls = []

    async def verify(self, **kwargs):
        self.calls.append(kwargs)
        raise AssertionError("proposal staging must not invoke semantic verifier")


def _context(participant_id, text):
    return AgentContext(
        participant_id=participant_id,
        participant_code="P-PERSONALIZATION",
        open_id="open",
        chat_id="chat",
        message_id="message",
        agent_run_id=uuid.uuid4(),
        user_request_text=text,
    )


def _stack(database, *, verifier=None):
    memory = MemoryService(ParticipantMemoryRepository(database))
    preferences = InteractionPreferenceService(
        InteractionPreferenceRepository(database),
        SupportPreferenceRepository(database),
    )
    proposals = PersonalizationProposalService(
        PersonalizationProposalRepository(database), memory, preferences
    )
    outbox = PresentationOutbox()
    registry = ToolRegistry(mutation_verifier=verifier)
    MemoryTools(memory, outbox, proposals).register(registry)
    InteractionPreferenceTools(preferences, outbox, proposals).register(registry)
    actions = CardActionService(
        ObservationRepository(database),
        observation_refresh=SimpleNamespace(),
        memory=memory,
        interaction_preferences=preferences,
        personalization_proposals=proposals,
    )
    return memory, preferences, proposals, outbox, registry, actions


def _action(card, name):
    def visit(value):
        if isinstance(value, dict):
            if value.get("mindflow_action") == name:
                return value
            for child in value.values():
                found = visit(child)
                if found is not None:
                    return found
        if isinstance(value, list):
            for child in value:
                found = visit(child)
                if found is not None:
                    return found
        return None

    result = visit(card)
    if result is None:
        raise AssertionError(f"missing {name}")
    return result


def test_memory_review_card_shows_exact_content_before_persist():
    database = memory_database()
    owner = participant(database, "MEMORY-PROPOSAL")
    verifier = _FailIfCalledVerifier()
    memory, _preferences, _proposals, outbox, registry, actions = _stack(
        database, verifier=verifier
    )
    ctx = _context(owner.id, "记住我喜欢周末晨跑")

    staged = asyncio.run(
        registry.execute(
            ctx,
            "memory_remember_explicit",
            {
                "memory_type": "routine",
                "content": "我喜欢周末晨跑",
            },
        )
    )

    assert staged.result["persisted"] is False
    assert memory.list(owner.id) == []
    assert verifier.calls == []
    card = outbox.take_cards(ctx.agent_run_id)[0]
    assert "我喜欢周末晨跑" in json.dumps(card, ensure_ascii=False)

    confirmed = actions.handle(
        owner.id,
        message_id="memory-review",
        callback_event_id="memory-confirm",
        action_value=_action(card, "personalization_proposal_confirm"),
        form_value={},
    )
    assert confirmed["ok"] is True
    assert [item["content"] for item in memory.list(owner.id)] == [
        "我喜欢周末晨跑"
    ]


def test_memory_review_cancel_persists_nothing():
    database = memory_database()
    owner = participant(database, "MEMORY-PROPOSAL-CANCEL")
    memory, _preferences, _proposals, outbox, registry, actions = _stack(database)
    ctx = _context(owner.id, "记住我要完成论文")
    asyncio.run(
        registry.execute(
            ctx,
            "memory_remember_explicit",
            {"memory_type": "goal", "content": "完成论文"},
        )
    )
    card = outbox.take_cards(ctx.agent_run_id)[0]
    cancelled = actions.handle(
        owner.id,
        message_id="memory-review",
        action_value=_action(card, "personalization_proposal_cancel"),
        form_value={},
    )
    assert cancelled["persisted"] is False
    assert memory.list(owner.id) == []


def test_preference_change_uses_review_card_before_update():
    database = memory_database()
    owner = participant(database, "PREFERENCE-PROPOSAL")
    _memory, preferences, _proposals, outbox, registry, actions = _stack(database)
    ctx = _context(owner.id, "以后回答简短一点")

    staged = asyncio.run(
        registry.execute(
            ctx, "interaction_preferences_update", {"verbosity": "concise"}
        )
    )
    assert staged.result["persisted"] is False
    assert preferences.get(owner.id)["verbosity"] == "balanced"
    card = outbox.take_cards(ctx.agent_run_id)[0]
    serialized = json.dumps(card, ensure_ascii=False)
    assert "回答长度" in serialized and "concise" in serialized

    result = actions.handle(
        owner.id,
        message_id="preference-review",
        action_value=_action(card, "personalization_proposal_confirm"),
        form_value={},
    )
    assert result["ok"] is True
    assert preferences.get(owner.id)["verbosity"] == "concise"


def test_personalization_proposal_is_participant_bound_and_receipted():
    database = memory_database()
    owner = participant(database, "PERSONALIZATION-OWNER")
    other = participant(database, "PERSONALIZATION-OTHER")
    memory, _preferences, _proposals, outbox, registry, actions = _stack(database)
    ctx = _context(owner.id, "记住我的目标")
    asyncio.run(
        registry.execute(
            ctx,
            "memory_remember_explicit",
            {"memory_type": "goal", "content": "完成项目"},
        )
    )
    card = outbox.take_cards(ctx.agent_run_id)[0]
    denied = actions.handle(
        other.id,
        message_id="copied-review",
        action_value=_action(card, "personalization_proposal_confirm"),
        form_value={},
    )
    assert denied["error"] == "personalization_proposal_not_found"
    assert memory.list(owner.id) == []
    assert card_action_spec(
        "personalization_proposal_confirm"
    ).replay_policy == "receipt_required"


def test_agent_personalization_writes_are_proposal_stage_only():
    database = memory_database()
    _stack_values = _stack(database)
    registry = _stack_values[4]
    specs = {spec.name: spec for spec in registry.specs}
    for name in (
        "memory_remember_explicit",
        "memory_delete",
        "memory_replace",
        "memory_clear_all",
        "interaction_preferences_update",
        "support_preferences_update",
    ):
        assert specs[name].effect == "proposal_stage"
        assert specs[name].authorization_requirement == "none"
