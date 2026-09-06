import asyncio
from types import SimpleNamespace
import uuid

from app.services.multimodal_turn_coordinator import (
    COMPLETED,
    PROCESSING,
    MultimodalTurnCoordinator,
)


def test_nearby_text_attaches_by_participant_and_chat():
    now = [100.0]
    coordinator = MultimodalTurnCoordinator(clock=lambda: now[0])
    alice = uuid.uuid4()
    bob = uuid.uuid4()

    async def scenario():
        opened = await coordinator.open_image(alice, "chat-a", SimpleNamespace(message_id="img"))
        now[0] = 101.0
        assert await coordinator.attach_text(alice, "chat-a", SimpleNamespace(text="看看")) is opened.turn
        assert await coordinator.attach_text(bob, "chat-a", SimpleNamespace(text="不要串线")) is None
        assert await coordinator.attach_text(alice, "chat-b", SimpleNamespace(text="不要串群")) is None
        return opened.turn

    turn = asyncio.run(scenario())
    assert [event.text for event in turn.attached_text_events] == ["看看"]


def test_six_second_text_associates_but_text_after_fifteen_seconds_does_not():
    now = [0.0]
    coordinator = MultimodalTurnCoordinator(clock=lambda: now[0])
    participant_id = uuid.uuid4()

    async def scenario():
        turn = (await coordinator.open_image(participant_id, "chat", object())).turn
        now[0] = 6.0
        assert await coordinator.attach_text(participant_id, "chat", "默认作息") is turn
        now[0] = 15.01
        assert await coordinator.attach_text(participant_id, "chat", "新消息") is None

    asyncio.run(scenario())


def test_debounce_moves_current_turn_to_processing_without_holding_lock():
    coordinator = MultimodalTurnCoordinator(debounce_seconds=0)
    participant_id = uuid.uuid4()

    async def scenario():
        turn = (await coordinator.open_image(participant_id, "chat", object())).turn
        assert await coordinator.wait_for_debounce(turn) is turn
        assert turn.state == PROCESSING
        return turn

    asyncio.run(scenario())


def test_second_image_wakes_first_as_image_only_and_starts_a_new_turn():
    coordinator = MultimodalTurnCoordinator(debounce_seconds=0)
    participant_id = uuid.uuid4()

    async def scenario():
        first = (await coordinator.open_image(participant_id, "chat", "first")).turn
        result = await coordinator.open_image(participant_id, "chat", "second")
        assert result.displaced_turn is first
        assert first.state == PROCESSING
        assert await coordinator.wait_for_debounce(first) is first
        assert await coordinator.wait_for_debounce(result.turn) is result.turn

    asyncio.run(scenario())


def test_recent_context_expires_and_never_contains_raw_image_data():
    now = [10.0]
    coordinator = MultimodalTurnCoordinator(
        debounce_seconds=0, recent_context_seconds=120, clock=lambda: now[0]
    )
    participant_id = uuid.uuid4()

    async def scenario():
        turn = (await coordinator.open_image(participant_id, "chat", object())).turn
        await coordinator.wait_for_debounce(turn)
        recent = await coordinator.complete(
            turn,
            image_message_id="om-image",
            image_key="opaque-key",
            image_kind="course_schedule",
            summary={"summary": "课程表"},
        )
        assert turn.state == COMPLETED
        assert not hasattr(recent, "data")
        assert await coordinator.recent_context(participant_id, "chat") is recent
        now[0] = 130.01
        assert await coordinator.recent_context(participant_id, "chat") is None

    asyncio.run(scenario())
