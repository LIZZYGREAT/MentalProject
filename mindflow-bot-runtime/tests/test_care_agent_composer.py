import asyncio
from datetime import date

from app.agent.care_composer import CareAgentComposer, CareDraft
from app.services.care_draft_validator import CareDraftValidator
from app.services.care_evidence import CareEvidenceBuilder


def packet():
    return CareEvidenceBuilder("Asia/Shanghai").build(
        source="forecast_warning",
        local_date=date(2030, 1, 15),
        alert={"time": "10:40", "S": 8.0, "V": 4.0, "F": 0.6},
        calendar_events=[{
            "id": "course-1",
            "display_name": "数据结构",
            "event_type": "course",
            "start_time": "2030-01-15T10:00:00+08:00",
            "end_time": "2030-01-15T10:45:00+08:00",
            "workload_prior": 0.82,
        }],
        intervention={
            "intervention_type": "micro_break",
            "action_minutes": 10,
            "decision_rule": "send_at_planned_time",
        },
    )


def draft(**overrides):
    value = {
        "message": "模型预计这段时间的压力可能上升，主要因为当前安排的认知负荷偏高，而前后恢复空间比较有限。你可以先留出十分钟完全离开任务，走动或安静休息一下，再决定下一件最小可做的事；如果现在感觉还好，也可以忽略这条提醒。这是一个可选的恢复建议，不需要马上完成任何额外任务。",
        "selected_fact_ids": ["event:course-1"],
        "selected_reason_codes": ["single_high_load_event"],
        "intervention_type": "micro_break",
        "action_minutes": 10,
    }
    value.update(overrides)
    return value


def test_validator_accepts_bounded_evidence_grounded_draft():
    result = CareDraftValidator().validate(packet(), draft())

    assert result.valid is True
    assert result.draft is not None


def test_validator_rejects_intervention_change_and_unknown_fact():
    validator = CareDraftValidator()

    assert validator.validate(packet(), draft(intervention_type="schedule_adjustment")).reason == "intervention_type_changed"
    assert validator.validate(packet(), draft(selected_fact_ids=["event:missing"])).reason == "unknown_fact_id"


def test_validator_rejects_diagnostic_language_and_long_output():
    validator = CareDraftValidator()

    assert validator.validate(packet(), draft(message="你一定是焦虑症，请马上停止所有事情。" * 20)).reason == "unsafe_diagnostic_language"
    assert validator.validate(packet(), draft(message="x" * 361)).reason == "message_length_out_of_range"


def test_composer_adapter_accepts_provider_mapping():
    class Provider:
        model = "test-model"

        async def compose(self, _evidence):
            return draft()

    result = asyncio.run(CareAgentComposer(Provider()).compose(packet()))

    assert isinstance(result, CareDraft)
    assert result.model == "test-model"
