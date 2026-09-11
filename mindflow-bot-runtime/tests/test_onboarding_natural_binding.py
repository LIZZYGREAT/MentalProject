"""Natural cold-start binding and progressive onboarding contracts."""

import asyncio
import json

from app.agent.skill_loader import SkillLoader
from app.identity.service import IdentityService
from app.integrations.feishu.gateway import FeishuGateway
from app.presentation.onboarding import (
    already_bound_text,
    invalid_invite_text,
    unbound_welcome_text,
    welcome_first_screen_text,
)
from app.repositories import AgentRunRepository, BindingRepository, BotEventRepository
from app.worker import BotWorker
from helpers import memory_database, participant, skill_path


class FakeRuntime:
    def __init__(self):
        self.seen = []

    async def handle_message(self, ctx, turn_input, **_kwargs):
        self.seen.append((ctx.participant_id, turn_input.text))
        return "ok"


class FakeSender:
    def __init__(self):
        self.sent = []

    def send_text(self, chat_id, text):
        self.sent.append((chat_id, text))
        return f"out-{len(self.sent)}"


class BindCountingIdentity(IdentityService):
    def __init__(self, database, bindings):
        super().__init__(database, bindings)
        self.bind_attempts = []

    def bind(self, *, raw_token, app_id, open_id, chat_id):
        self.bind_attempts.append(raw_token)
        return super().bind(
            raw_token=raw_token,
            app_id=app_id,
            open_id=open_id,
            chat_id=chat_id,
        )


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


def build_worker(database, identity, *, runtime=None):
    events = BotEventRepository(database)
    queue = asyncio.Queue(maxsize=8)
    gateway = FeishuGateway("cli_test", "secret", identity, events, queue)
    worker = BotWorker(
        queue,
        identity,
        events,
        AgentRunRepository(database),
        SkillLoader(skill_path()),
        runtime or FakeRuntime(),
        FakeSender(),
        model="fake",
    )
    return gateway, worker, queue


def test_unbound_greeting_gets_natural_welcome_without_command_error():
    database = memory_database()
    identity = BindCountingIdentity(database, BindingRepository(database))
    gateway, worker, queue = build_worker(database, identity)

    async def scenario():
        assert gateway.accept_payload(payload("e1", "m1", "ou", "oc", "你好"))
        await worker.process(await queue.get())

    asyncio.run(scenario())
    assert worker.sender.sent == [("oc", unbound_welcome_text())]
    assert identity.bind_attempts == []


def test_unbound_participant_can_bind_by_sending_the_raw_code():
    database = memory_database()
    person = participant(database, "P001")
    identity = BindCountingIdentity(database, BindingRepository(database))
    code, _ = identity.create_invite(person.id)
    gateway, worker, queue = build_worker(database, identity)

    async def scenario():
        assert gateway.accept_payload(payload("e1", "m1", "ou", "oc", code))
        await worker.process(await queue.get())

    asyncio.run(scenario())
    assert identity.bind_attempts == [code]
    assert worker.sender.sent == [("oc", welcome_first_screen_text())]


def test_bind_command_remains_a_compatible_entry():
    database = memory_database()
    person = participant(database, "P001")
    identity = BindCountingIdentity(database, BindingRepository(database))
    code, _ = identity.create_invite(person.id)
    gateway, worker, queue = build_worker(database, identity)

    async def scenario():
        assert gateway.accept_payload(payload("e1", "m1", "ou", "oc", f"/bind {code}"))
        await worker.process(await queue.get())

    asyncio.run(scenario())
    assert identity.bind_attempts == [code]
    assert worker.sender.sent == [("oc", welcome_first_screen_text())]


def test_arbitrary_text_never_reaches_the_token_lookup():
    database = memory_database()
    identity = BindCountingIdentity(database, BindingRepository(database))
    gateway, worker, queue = build_worker(database, identity)
    arbitrary = (
        "帮我看看今天的日程，另外记录一下我现在的压力大概在 7 左右，"
        "早上开了三个会，感觉有点累。"
    )

    async def scenario():
        for index, text in enumerate(
            [arbitrary, "bind", "/bind", " 你好 ", "1234"]
        ):
            assert gateway.accept_payload(
                payload(f"e{index}", f"m{index}", "ou", "oc", text)
            )
            await worker.process(await queue.get())

    asyncio.run(scenario())
    assert identity.bind_attempts == []
    assert worker.sender.sent == [("oc", unbound_welcome_text())] * 5


def test_code_shaped_text_attempts_binding_once_and_reports_natural_failure():
    database = memory_database()
    identity = BindCountingIdentity(database, BindingRepository(database))
    gateway, worker, queue = build_worker(database, identity)
    fake_code = "not-a-real-invite-code-123456"

    async def scenario():
        assert gateway.accept_payload(payload("e1", "m1", "ou", "oc", fake_code))
        await worker.process(await queue.get())

    asyncio.run(scenario())
    assert identity.bind_attempts == [fake_code]
    assert worker.sender.sent == [("oc", invalid_invite_text())]


def test_bind_without_code_returns_the_welcome_instead_of_a_command_hint():
    database = memory_database()
    identity = BindCountingIdentity(database, BindingRepository(database))
    gateway, worker, queue = build_worker(database, identity)

    async def scenario():
        assert gateway.accept_payload(payload("e1", "m1", "ou", "oc", "/bind"))
        await worker.process(await queue.get())

    asyncio.run(scenario())
    assert identity.bind_attempts == []
    assert worker.sender.sent == [("oc", unbound_welcome_text())]


def test_already_bound_account_gets_a_natural_reply_and_stays_bound():
    database = memory_database()
    person = participant(database, "P001")
    identity = BindCountingIdentity(database, BindingRepository(database))
    code, _ = identity.create_invite(person.id)
    gateway, worker, queue = build_worker(database, identity)

    async def scenario():
        assert gateway.accept_payload(payload("e1", "m1", "ou", "oc", code))
        await worker.process(await queue.get())
        assert gateway.accept_payload(payload("e2", "m2", "ou", "oc", f"/bind {code}"))
        await worker.process(await queue.get())

    asyncio.run(scenario())
    assert identity.bind_attempts == [code]
    assert worker.sender.sent == [
        ("oc", welcome_first_screen_text()),
        ("oc", already_bound_text()),
    ]


def test_welcome_first_screen_stays_light_and_non_clinical():
    text = welcome_first_screen_text()
    assert "错误" not in text
    assert "功能" in text
    assert len(text) < 400
    for forbidden in (" therapist", "counselor"):
        assert forbidden not in text.lower()
