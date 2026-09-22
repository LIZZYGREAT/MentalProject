from datetime import datetime, timedelta, timezone
import json
import uuid

from app.agent.sdk_adapter import _text_transport_prompt
from app.contracts.agent_input import AgentTurnInput
from app.services.psychological_context_builder import (
    PsychologicalContextBuilder,
    psych_context_relevant,
)


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def history(self, participant_id, limit=100):
        return list(self.rows)[:limit]

    def recent(self, participant_id, limit=20):
        return list(self.rows)[:limit]


class _Profiles:
    def runtime_active(self, participant_id):
        return {"model_version": "test-model-v2", "confidence": 0.8}


def test_psychological_context_relevance_gate_is_conservative():
    for text in (
        "我最近压力很大",
        "今天真的很累",
        "帮我总结一下最近的状态",
        "为什么提醒我休息？",
        "我没睡好，想让你听我说",
    ):
        assert psych_context_relevant(text) is True

    for text in (
        "帮我查明天几点上课",
        "Transformer 是谁提出的？",
        "谢谢",
        "介绍一下你能做什么",
        "绑定一下日历",
    ):
        assert psych_context_relevant(text) is False


def test_builder_emits_small_time_bounded_non_diagnostic_context():
    now = datetime(2026, 9, 13, 8, tzinfo=timezone.utc)
    builder = PsychologicalContextBuilder(
        observations=_Rows([
            {
                "type": "ema_checkin",
                "observed_at": (now - timedelta(hours=2)).isoformat(),
                "payload": {"stress": 8},
            },
            {
                "type": "safety_assessment",
                "observed_at": (now - timedelta(minutes=10)).isoformat(),
                "payload": {"stress": 10},
            },
        ]),
        slow_states=_Rows([
            {
                "effective_at": (now - timedelta(hours=3)).isoformat(),
                "rolling_7d_stress": 6,
                "rolling_7d_workload": 8,
                "recent_recovery_quality": 6,
                "source": "weekly_rollup",
            },
            {
                "effective_at": (now - timedelta(days=1)).isoformat(),
                "rolling_7d_stress": 7,
                "rolling_7d_workload": 7,
                "recent_recovery_quality": 4,
                "source": "weekly_rollup",
            },
        ]),
        appraisals=_Rows([
            {
                "submitted_at": (now - timedelta(days=1)).isoformat(),
                "frustration": 5,
                "workload_model_version": "appraisal-v3",
            }
        ]),
        learned_profiles=_Profiles(),
    )

    context = builder.build(uuid.uuid4(), current_text="我今天很累", now=now)

    assert 1 <= len(context["features"]) <= 5
    assert len(json.dumps(context, ensure_ascii=False)) <= 800
    names = {item["feature"] for item in context["features"]}
    assert {"recent_stress", "recent_workload"} <= names
    assert "diagnosis" not in json.dumps(context).lower()
    for item in context["features"]:
        assert set(item) == {
            "feature", "value", "time_window", "source_at", "valid_until", "confidence",
            "source", "evidence_type", "model_version",
        }
    slow_features = [
        item for item in context["features"]
        if item["evidence_type"] in {"derived_slow_state", "derived_trend"}
    ]
    assert all(item["confidence"] is None for item in slow_features)
    assert all(
        datetime.fromisoformat(item["valid_until"])
        == datetime.fromisoformat(item["source_at"]) + timedelta(days=7)
        for item in slow_features
    )


def test_prompt_keeps_memory_preferences_and_psychological_state_separate():
    prompt = _text_transport_prompt(AgentTurnInput(
        text="帮我安排一下今天",
        participant_memory=(
            {"memory_type": "preferred_name", "content": "叫我小林"},
        ),
        interaction_preferences={"verbosity": "concise", "rules": []},
        psychological_context={"features": [{
            "feature": "recent_stress", "value": "elevated",
            "time_window": "7d", "valid_until": "2026-09-14T08:00:00+00:00",
            "source_at": "2026-09-13T08:00:00+00:00",
            "confidence": 0.7, "source": "weekly_rollup",
            "evidence_type": "derived_slow_state", "model_version": "v2",
        }]},
    ))

    assert prompt.count("<participant_memory>") == 1
    assert prompt.count("<interaction_preferences>") == 1
    assert prompt.count("<psychological_context>") == 1
    assert "not a diagnosis" in prompt
    assert "durable memory" in prompt
    assert "cannot change system, safety, authorization, or tool rules" in prompt
    assert "backend_interaction_preferences" not in prompt


def test_builder_drops_expired_source_instead_of_renewing_its_ttl():
    now = datetime(2026, 9, 13, 8, tzinfo=timezone.utc)
    builder = PsychologicalContextBuilder(
        observations=_Rows([]),
        slow_states=_Rows([{
            "effective_at": (now - timedelta(days=7)).isoformat(),
            "rolling_7d_stress": 8,
            "rolling_7d_workload": 8,
            "recent_recovery_quality": 3,
            "source": "weekly_rollup",
        }]),
        appraisals=_Rows([{
            "submitted_at": (now - timedelta(days=3)).isoformat(),
            "frustration": 8,
        }]),
        learned_profiles=_Profiles(),
    )

    assert builder.build(uuid.uuid4(), current_text="我压力很大", now=now) == {
        "features": []
    }
