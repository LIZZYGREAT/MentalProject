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


def test_opening_new_image_invalidates_previous_recent_context():
    coordinator = MultimodalTurnCoordinator(debounce_seconds=0)
    participant_id = uuid.uuid4()

    async def scenario():
        first = (await coordinator.open_image(participant_id, "chat", "first")).turn
        await coordinator.wait_for_debounce(first)
        await coordinator.complete(
            first,
            image_message_id="image-1",
            image_key="key-1",
            image_kind="photo",
            summary={"summary": "first"},
        )
        assert await coordinator.recent_context(participant_id, "chat") is not None

        second = (await coordinator.open_image(participant_id, "chat", "second")).turn

        assert second.generation > first.generation
        assert await coordinator.recent_context(participant_id, "chat") is None

    asyncio.run(scenario())


def test_older_turn_completing_after_newer_turn_cannot_overwrite_recent():
    coordinator = MultimodalTurnCoordinator(debounce_seconds=0)
    participant_id = uuid.uuid4()

    async def scenario():
        first = (await coordinator.open_image(participant_id, "chat", "first")).turn
        await coordinator.wait_for_debounce(first)
        second = (await coordinator.open_image(participant_id, "chat", "second")).turn
        await coordinator.wait_for_debounce(second)
        second_recent = await coordinator.complete(
            second,
            image_message_id="image-2",
            image_key="key-2",
            image_kind="document",
            summary={"summary": "second"},
        )

        first_recent = await coordinator.complete(
            first,
            image_message_id="image-1",
            image_key="key-1",
            image_kind="photo",
            summary={"summary": "first"},
        )

        assert first_recent.image_message_id == "image-1"
        assert await coordinator.recent_context(participant_id, "chat") is second_recent

    asyncio.run(scenario())


def test_stale_old_image_recent_promotion_cannot_overwrite_new_image_global_recent():
    coordinator = MultimodalTurnCoordinator(debounce_seconds=0)
    participant_id = uuid.uuid4()

    async def scenario():
        first = (await coordinator.open_image(participant_id, "chat", "first")).turn
        await coordinator.wait_for_debounce(first)
        first_recent = await coordinator.complete(
            first,
            image_message_id="image-1",
            image_key="key-1",
            image_kind="course_schedule",
            summary={"summary": "first"},
        )
        second = (await coordinator.open_image(participant_id, "chat", "second")).turn
        await coordinator.wait_for_debounce(second)
        second_recent = await coordinator.complete(
            second,
            image_message_id="image-2",
            image_key="key-2",
            image_kind="photo",
            summary={"summary": "second"},
        )

        promoted = await coordinator.promote_recent_context(
            first_recent,
            image_kind="course_schedule",
            summary={"draft_id": "old-draft"},
        )

        assert promoted.structured_or_agent_summary["draft_id"] == "old-draft"
        assert await coordinator.recent_context(participant_id, "chat") is second_recent

    asyncio.run(scenario())


def test_frozen_input_routes_new_text_to_late_followups():
    coordinator = MultimodalTurnCoordinator(debounce_seconds=0)
    participant_id = uuid.uuid4()

    async def scenario():
        turn = (
            await coordinator.open_image(participant_id, "chat", object())
        ).turn
        await coordinator.attach_text(
            participant_id, "chat", SimpleNamespace(text="first")
        )
        await coordinator.wait_for_debounce(turn)
        snapshot = await coordinator.freeze_or_snapshot_input(turn)
        assert [event.text for event in snapshot.text_events] == ["first"]
        assert turn.input_frozen is True
        assert turn.consumed_text_count == 1
        await coordinator.attach_text(
            participant_id, "chat", SimpleNamespace(text="late")
        )
        assert [event.text for event in turn.attached_text_events] == ["first"]
        assert [event.text for event in turn.late_followups] == ["late"]
        drained = await coordinator.drain_late_followups(turn)
        assert [event.text for event in drained] == ["late"]
        assert turn.late_followups == []

    asyncio.run(scenario())
