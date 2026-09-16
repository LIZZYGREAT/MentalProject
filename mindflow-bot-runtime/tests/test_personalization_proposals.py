import asyncio
import json
from types import SimpleNamespace
import uuid

from app.agent.context import AgentContext
from app.agent.tool_registry import ToolRegistry
from app.card_actions.registry import card_action_spec
from app.repositories import ObservationRepository
from app.repositories_memory import ParticipantMemoryRepository
from app.repositories_care import ParticipantCarePreferenceRepository
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
from app.tools.care import CareTools
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


def _care_stack(database, *, verifier=None):
    memory = MemoryService(ParticipantMemoryRepository(database))
    preferences = InteractionPreferenceService(
        InteractionPreferenceRepository(database),
        SupportPreferenceRepository(database),
    )
    care_preferences = ParticipantCarePreferenceRepository(
        database,
        system_max_daily_sends=3,
        timezone_name="Asia/Shanghai",
    )
    proposals = PersonalizationProposalService(
        PersonalizationProposalRepository(database),
        memory,
        preferences,
        care_preferences,
    )
    outbox = PresentationOutbox()
    registry = ToolRegistry(mutation_verifier=verifier)
    CareTools(
        None,
        ObservationRepository(database),
        None,
        None,
        "Asia/Shanghai",
        None,
        presentations=outbox,
        care_preferences=care_preferences,
        personalization_proposals=proposals,
    ).register(registry)
    actions = CardActionService(
        ObservationRepository(database),
        observation_refresh=SimpleNamespace(),
        care_preferences=care_preferences,
        personalization_proposals=proposals,
    )
    return care_preferences, outbox, registry, actions


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


def test_care_preference_change_uses_compact_review_card_before_update():
    database = memory_database()
    owner = participant(database, "CARE-PREFERENCE-PROPOSAL")
    verifier = _FailIfCalledVerifier()
    care_preferences, outbox, registry, actions = _care_stack(
        database, verifier=verifier
    )
    ctx = _context(owner.id, "早报改到八点半，晚上十一点后不要打扰")
    spec = next(
        item for item in registry.specs if item.name == "care_update_preferences"
    )
    assert spec.effect == "proposal_stage"
    assert spec.authorization_requirement == "none"

    staged = asyncio.run(registry.execute(ctx, "care_update_preferences", {
        "morning_brief_enabled": True,
        "morning_brief_local_time": "08:30",
        "quiet_hours_start": "23:00",
        "quiet_hours_end": "07:00",
        "max_proactive_care_per_day": 1,
    }))

    assert staged.result["persisted"] is False
    assert verifier.calls == []
    before = care_preferences.get(owner.id)
    assert before["morning_brief_enabled"] is False
    assert before["quiet_hours_start"] is None
    card = outbox.take_cards(ctx.agent_run_id)[0]
    serialized = json.dumps(card, ensure_ascii=False)
    for expected in ("08:30", "23:00", "07:00", "每日主动关怀上限"):
        assert expected in serialized

    confirmed = actions.handle(
        owner.id,
        message_id="care-preference-review",
        callback_event_id="care-preference-confirm",
        action_value=_action(card, "personalization_proposal_confirm"),
        form_value={},
    )

    assert confirmed["ok"] is True
    assert confirmed["reply_text"] == "提醒与关怀设置已更新。"
    after = care_preferences.get(owner.id)
    assert after["morning_brief_enabled"] is True
    assert after["morning_brief_local_time"] == "08:30"
    assert after["quiet_hours_start"] == "23:00"
    assert after["quiet_hours_end"] == "07:00"
    assert after["max_proactive_care_per_day"] == 1


def test_care_preference_review_cancel_persists_nothing():
    database = memory_database()
    owner = participant(database, "CARE-PREFERENCE-CANCEL")
    care_preferences, outbox, registry, actions = _care_stack(database)
    ctx = _context(owner.id, "关闭支持性跟进")

    asyncio.run(registry.execute(
        ctx, "care_update_preferences", {"allow_follow_up": False}
    ))
    card = outbox.take_cards(ctx.agent_run_id)[0]
    cancelled = actions.handle(
        owner.id,
        message_id="care-preference-review",
        callback_event_id="care-preference-cancel",
        action_value=_action(card, "personalization_proposal_cancel"),
        form_value={},
    )

    assert cancelled["persisted"] is False
    assert care_preferences.get(owner.id)["allow_follow_up"] is True


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
