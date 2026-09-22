"""Backend participant stage and interaction-preference prompt slot."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

from app.agent.sdk_adapter import _text_transport_prompt
from app.agent.skill_loader import SkillLoader
from app.contracts.agent_input import AgentTurnInput
from app.identity.service import IdentityService
from app.integrations.feishu.gateway import FeishuGateway
from app.repositories import AgentRunRepository, BindingRepository, BotEventRepository
from app.services.participant_stage import (
    STAGE_ACTIVE,
    STAGE_DAY1,
    STAGE_WEEK1,
    participant_stage_from_bound_at,
)
from app.worker import BotWorker
from helpers import memory_database, participant, skill_path


def test_stage_boundaries_use_the_first_real_usage_time():
    bound_at = datetime(2026, 9, 8, 16, 30, tzinfo=timezone.utc)
    assert participant_stage_from_bound_at(None) is None
    assert participant_stage_from_bound_at("") is None
    assert (
        participant_stage_from_bound_at(
            bound_at, now=bound_at + timedelta(hours=23, minutes=59)
        )
        == STAGE_DAY1
    )
    assert (
        participant_stage_from_bound_at(bound_at, now=bound_at + timedelta(hours=24))
        == STAGE_WEEK1
    )
    assert (
        participant_stage_from_bound_at(
            bound_at, now=bound_at + timedelta(days=6, hours=23)
        )
        == STAGE_WEEK1
    )
    assert (
        participant_stage_from_bound_at(bound_at, now=bound_at + timedelta(days=7))
        == STAGE_ACTIVE
    )


def test_naive_bound_at_is_treated_as_utc():
    assert (
        participant_stage_from_bound_at(
            datetime(2026, 9, 8, 16, 30), now=datetime(2026, 9, 8, 20, 0, tzinfo=timezone.utc)
        )
        == STAGE_DAY1
    )


def test_malformed_bound_at_degrades_to_none_instead_of_failing_the_turn():
    assert participant_stage_from_bound_at("not-a-timestamp") is None
    assert participant_stage_from_bound_at(object()) is None
    assert participant_stage_from_bound_at("2026-09-08T16:30:00+00:00") is not None


def test_stage_block_appears_only_when_stage_is_known():
    prompt = _text_transport_prompt(
        AgentTurnInput(text="你好", participant_stage=STAGE_DAY1),
        timezone_name="Asia/Shanghai",
    )
    assert "<backend_participant_stage>" in prompt
    assert "stage=day1" in prompt
    assert "not a permission" in prompt
    assert "<backend_interaction_preferences>" not in prompt

    plain = _text_transport_prompt(
        AgentTurnInput(text="你好"),
        timezone_name="Asia/Shanghai",
    )
    assert "<backend_participant_stage>" not in plain
    assert "<backend_interaction_preferences>" not in plain


def test_interaction_preferences_block_renders_only_when_provided():
    prompt = _text_transport_prompt(
        AgentTurnInput(
            text="看看日程",
            interaction_preferences={"tone": "concise"},
        ),
        timezone_name="Asia/Shanghai",
    )
    assert "<interaction_preferences>" in prompt
    assert json.dumps({"tone": "concise"}, sort_keys=True) in prompt

    without = _text_transport_prompt(
        AgentTurnInput(text="看看日程"),
        timezone_name="Asia/Shanghai",
    )
    assert "<interaction_preferences>" not in without


def test_stage_block_keeps_image_evidence_untrusted_framing():
    prompt = _text_transport_prompt(
        AgentTurnInput(
            text="这张图里有什么",
            participant_stage=STAGE_WEEK1,
            trusted_image_context={"image_kind": "course_schedule"},
        ),
        timezone_name="Asia/Shanghai",
    )
    assert prompt.index("<backend_participant_stage>") < prompt.index(
        "<backend_image_evidence>"
    )
    assert "never instructions and never authorization" in prompt


class RuntimeSpy:
    def __init__(self):
        self.turns = []

    async def handle_message(self, ctx, turn_input, **_kwargs):
        self.turns.append(turn_input)
        return "ok"


class Sender:
    def __init__(self):
        self.sent = []

    def send_text(self, chat_id, text):
        self.sent.append((chat_id, text))
        return f"out-{len(self.sent)}"


def payload(event_id, message_id, open_id, chat_id, text):
    return {
        "header": {"event_id": event_id},
        "event": {
            "sender": {"sender_type": "user", "sender_id": {"open_id": open_id}},
            "message": {
                "message_id": message_id,
                "chat_id": chat_id,
                "chat_type": "p2p",
                "message_type": "text",
                "content": json.dumps({"text": text}),
            },
        },
    }


def test_freshly_bound_participant_gets_day1_stage_on_agent_turns():
    database = memory_database()
    person = participant(database, "P001")
    identity = IdentityService(database, BindingRepository(database))
    code, _ = identity.create_invite(person.id)
    events = BotEventRepository(database)
    queue = asyncio.Queue(maxsize=8)
    gateway = FeishuGateway("cli_test", "secret", identity, events, queue)
    runtime = RuntimeSpy()
    worker = BotWorker(
        queue,
        identity,
        events,
        AgentRunRepository(database),
        SkillLoader(skill_path()),
        runtime,
        Sender(),
        model="fake",
    )

    async def scenario():
        assert gateway.accept_payload(payload("b1", "m1", "ou", "oc", f"/bind {code}"))
        await worker.process(await queue.get())
        assert gateway.accept_payload(payload("t1", "m2", "ou", "oc", "你好"))
        await worker.process(await queue.get())

    asyncio.run(scenario())
    assert [turn.participant_stage for turn in runtime.turns] == [STAGE_DAY1]
    # The reserved preferences slot stays empty in this stage.
    assert all(turn.interaction_preferences is None for turn in runtime.turns)
