from datetime import datetime, timezone
import uuid

import pytest

from app.agent.context import AgentContext
from app.agent.tool_registry import ToolRegistry
from app.identity.service import BindingError, IdentityService
from app.repositories import (
    BindingRepository,
    ConversationRepository,
    ObservationRepository,
    PredictionRepository,
    ProfileRepository,
)
from helpers import memory_database, participant


def test_one_time_binding_and_identity_isolation():
    database = memory_database()
    p1 = participant(database, "P001")
    p2 = participant(database, "P002")
    bindings = BindingRepository(database)
    identity = IdentityService(database, bindings)
    code1, _ = identity.create_invite(p1.id)
    code2, _ = identity.create_invite(p2.id)

    bound1 = identity.bind(
        raw_token=code1,
        app_id="cli_test",
        open_id="ou_1",
        chat_id="oc_1",
    )
    assert bound1.id == p1.id
    assert identity.resolve("cli_test", "ou_1").id == p1.id
    assert identity.resolve("cli_test", "ou_2") is None
    with pytest.raises(BindingError):
        identity.bind(
            raw_token=code1,
            app_id="cli_test",
            open_id="ou_other",
            chat_id="oc_other",
        )
    bound2 = identity.bind(
        raw_token=code2,
        app_id="cli_test",
        open_id="ou_2",
        chat_id="oc_2",
    )
    assert bound2.id == p2.id


def test_profile_observation_prediction_and_conversation_are_scoped():
    database = memory_database()
    p1 = participant(database, "P001")
    p2 = participant(database, "P002")
    profiles = ProfileRepository(database)
    observations = ObservationRepository(database)
    predictions = PredictionRepository(database)
    conversations = ConversationRepository(database)

    profiles.save(p1.id, {"memory": "apple"})
    profiles.save(p2.id, {"memory": "banana"})
    observations.add(p1.id, "checkin", {"stress_0_10": 2})
    observations.add(p2.id, "checkin", {"stress_0_10": 8})
    predictions.save(
        p1.id,
        profile_version=1,
        model_version="test",
        input_snapshot={},
        output={"marker": "apple"},
    )
    predictions.save(
        p2.id,
        profile_version=1,
        model_version="test",
        input_snapshot={},
        output={"marker": "banana"},
    )
    conversations.add(p1.id, "user", "remember apple")
    conversations.add(p2.id, "user", "remember banana")

    assert profiles.current(p1.id)["profile"]["memory"] == "apple"
    assert observations.recent(p1.id)[0]["payload"]["stress_0_10"] == 2
    assert predictions.latest(p1.id)["output"]["marker"] == "apple"
    assert conversations.recent(p1.id, 10)[0]["content"] == "remember apple"
    assert "banana" not in str(conversations.recent(p1.id, 10))


def test_tool_schema_rejects_cross_user_argument():
    registry = ToolRegistry()

    def handler(ctx, args):
        return {"participant": str(ctx.participant_id), "value": args["value"]}

    registry.register(
        "safe_tool",
        "test",
        {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        handler,
    )
    trusted_id = uuid.uuid4()
    ctx = AgentContext(trusted_id, "P001", "ou_1", "oc_1", "m1", uuid.uuid4())
    result = __import__("asyncio").run(
        registry.execute(
            ctx,
            "safe_tool",
            {"value": "ok", "participant_id": str(uuid.uuid4())},
        )
    )
    assert result.status == "invalid_arguments"
    assert result.result["error"] == "invalid_arguments"

    with pytest.raises(ValueError, match="forbidden"):
        registry.register(
            "unsafe_tool",
            "test",
            {
                "type": "object",
                "properties": {"participant_id": {"type": "string"}},
                "additionalProperties": False,
            },
            handler,
        )
