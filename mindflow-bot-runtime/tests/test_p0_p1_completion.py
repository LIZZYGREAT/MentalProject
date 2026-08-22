import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
import threading
import uuid
from zoneinfo import ZoneInfo

import pytest

from app.agent.context import AgentContext
from app.db import Database, build_engine
from app.integrations.feishu.cards import pressure_curve_card
from app.integrations.feishu.client import FeishuClient, FeishuSendError
from app.integrations.feishu.gateway import FeishuCardActionAdapter
from mindflow_core.assessment import AssessmentModel
from app.models import StateObservation, WarningSchedule
from app.repositories import (
    ObservationRepository,
    ParticipantRepository,
)
from app.services.card_action_service import CardActionService
from app.services.curve_analysis import analyze_curve
from app.services.pressure_curve_renderer import PressureCurveRenderer
from app.services.presentation_service import (
    IMAGE_KEY_PLACEHOLDER,
    PendingImageCard,
)
from app.tools.care import CareTools
from app.worker import BotWorker
from helpers import memory_database, participant
from tests.test_forecast_hardening import (
    TEST_LOCAL_DATE,
    TEST_NOW,
    save_forecast_and_warnings,
)
from tests.test_forecast_pipeline import build_pipeline, event


def _refresh_terminal_warning(status: str, attempt_count: int) -> WarningSchedule:
    database, person, _, _, _, warnings, coordinator = build_pipeline([event()])
    asyncio.run(coordinator.ensure_forecast(person.id, TEST_LOCAL_DATE, "prepare"))
    with database.session() as session:
        row = session.query(WarningSchedule).one()
        warning_id = row.id
        episode = row.episode_identity
        target = TEST_NOW + timedelta(minutes=5)
        valid_until = TEST_NOW + timedelta(minutes=15)
        risk = TEST_NOW + timedelta(minutes=25)
        row.target_time = target
        row.valid_until = valid_until
        row.risk_time = risk
        row.status = status
        row.attempt_count = attempt_count
        row.next_attempt_at = None
    save_forecast_and_warnings(
        database,
        person,
        warnings,
        version=f"metadata-{status}-{attempt_count}",
        items=[{
            "warning_identity": episode,
            "episode_identity": episode,
            "target_time": target,
            "valid_until": valid_until,
            "risk_time": risk,
            "warning_level": "2",
            "episode_drift_minutes": 15,
            "payload": {"message": "metadata changed"},
        }],
    )
    with database.session() as session:
        row = session.get(WarningSchedule, warning_id)
        session.expunge(row)
        return row


def test_failed_warning_is_not_reactivated_by_metadata_refresh():
    assert _refresh_terminal_warning("failed", 1).status == "failed"


def test_max_attempt_warning_is_not_reactivated_by_same_episode_refresh():
    row = _refresh_terminal_warning("failed", 5)
    assert row.status == "failed"
    assert row.attempt_count == 5
    assert row.next_attempt_at is None


def test_nonretryable_warning_is_not_reactivated_by_forecast_refresh():
    row = _refresh_terminal_warning("failed", 1)
    assert row.status == "failed"
    assert row.next_attempt_at is None


def _card_event(stress: str, energy: str = "4"):
    raw = SimpleNamespace(
        message_id="om-card",
        chat_id="oc-chat",
        operator=SimpleNamespace(open_id="ou-user"),
        action=SimpleNamespace(
            tag="button",
            value={"mindflow_action": "submit_checkin", "version": "1"},
            form_value={
                "stress": stress,
                "energy": energy,
                "activity": "写课程作业",
                "stress_event_since_last": "true",
                "event_ongoing": "false",
            },
        ),
    )
    return FeishuCardActionAdapter("app").adapt(raw)


def _handle_card(service, person_id, card_event):
    return service.handle(
        person_id,
        message_id=card_event.message_id,
        callback_event_id=card_event.event_id,
        action_value=card_event.action_value,
        form_value=card_event.form_value,
    )


def test_duplicate_identical_card_callback_is_idempotent():
    database = memory_database()
    person = participant(database, "CARD-IDENTICAL")
    repository = ObservationRepository(database)
    service = CardActionService(repository)
    card_event = _card_event("7")
    first = _handle_card(service, person.id, card_event)
    second = _handle_card(service, person.id, card_event)
    assert first["observation_id"] == second["observation_id"]
    assert len(repository.recent(person.id)) == 1


def test_same_card_with_changed_form_values_records_new_observation():
    database = memory_database()
    person = participant(database, "CARD-CHANGED")
    repository = ObservationRepository(database)
    service = CardActionService(repository)
    first = _handle_card(service, person.id, _card_event("7", "4"))
    second = _handle_card(service, person.id, _card_event("2", "9"))
    assert first["observation_id"] != second["observation_id"]
    payloads = [item["payload"] for item in repository.recent(person.id)]
    assert {(item["stress_0_10"], item["energy_0_10"]) for item in payloads} == {
        (7.0, 4.0),
        (2.0, 9.0),
    }


def test_concurrent_duplicate_card_callbacks_are_idempotent(tmp_path):
    database = Database(
        build_engine(f"sqlite:///{(tmp_path / 'callbacks.sqlite3').as_posix()}")
    )
    database.create_schema_for_tests()
    person = ParticipantRepository(database).create("CARD-CONCURRENT")
    service = CardActionService(ObservationRepository(database))
    card_event = _card_event("6")
    barrier = threading.Barrier(6)

    def submit():
        barrier.wait()
        return _handle_card(service, person.id, card_event)["observation_id"]

    with ThreadPoolExecutor(max_workers=6) as executor:
        ids = list(executor.map(lambda _index: submit(), range(6)))
    assert len(set(ids)) == 1
    with database.session() as session:
        assert session.query(StateObservation).count() == 1


def test_recent_checkin_affects_initial_stress():
    stress, _vitality = AssessmentModel._latest_state([{
        "payload": {"stress_0_10": 8.5, "energy_0_10": 3.0}
    }])
    assert stress == 85.0


def test_recent_checkin_affects_initial_vitality():
    _stress, vitality = AssessmentModel._latest_state([{
        "payload": {"stress_0_10": 8.5, "energy_0_10": 3.0}
    }])
    assert vitality == 30.0


def test_forecast_changes_after_new_state_observation():
    database, person, _calendar, _semantics, _prediction, _warnings, coordinator = (
        build_pipeline([])
    )

    class ObservationPrediction:
        model = SimpleNamespace(MODEL_VERSION="observation-aware-v1")

        def __init__(self):
            self.calls = 0

        def calculate(self, **kwargs):
            self.calls += 1
            observations = kwargs["observations"]
            payload = observations[0]["payload"] if observations else {}
            stress = float(payload.get("stress_0_10", 4.0))
            vitality = float(payload.get("energy_0_10", 7.0))
            return {
                "model_version": self.model.MODEL_VERSION,
                "local_date": kwargs["local_date"],
                "stress_0_10": stress,
                "vitality_0_10": vitality,
                "trajectory": [{
                    "time": "00:00",
                    "stress_0_10": stress,
                    "vitality_0_10": vitality,
                }],
                "alerts": [],
            }

    prediction = ObservationPrediction()
    coordinator.prediction = prediction
    before = asyncio.run(
        coordinator.ensure_forecast(person.id, TEST_LOCAL_DATE, "before_checkin")
    )
    ObservationRepository(database).add(
        person.id,
        "checkin",
        {"stress_0_10": 9.0, "energy_0_10": 2.0},
        observed_at=TEST_NOW,
        source_message_id="new-checkin",
    )
    after = asyncio.run(
        coordinator.ensure_forecast(person.id, TEST_LOCAL_DATE, "after_checkin")
    )
    assert prediction.calls == 2
    assert before["forecast_version"] != after["forecast_version"]
    assert before["curve"][0]["stress_0_10"] == 4.0
    assert after["curve"][0] == {
        "time": "00:00",
        "stress_0_10": 9.0,
        "vitality_0_10": 2.0,
    }


def _full_curve():
    result = []
    for index in range(288):
        event_active = 108 <= index < 126
        result.append({
            "time": f"{index // 12:02d}:{(index % 12) * 5:02d}",
            "stress_0_10": 9.4 if index == 250 else 3.0 + index / 200,
            "vitality_0_10": 8.0 - index / 100,
            "confidence_0_1": min(1.0, index / 287),
            "continuous_load_penalty": 0.6 if 120 <= index < 150 else 0.0,
            "stress_equilibrium_0_10": 7.0 if event_active else 5.0,
            "stress_interval_90_0_10": {
                "lower": max(0.0, 2.5 + index / 200),
                "upper": min(10.0, 3.5 + index / 200),
            },
            "event_stress_input": 0.7 if event_active else 0.0,
            "anticipatory_input": 0.3 if 96 <= index < 108 else 0.0,
            "post_event_input": 0.4 if 126 <= index < 144 else 0.0,
        })
    return result


def test_curve_analysis_uses_all_288_points():
    assert analyze_curve(_full_curve()).point_count == 288


def test_late_day_peak_is_detected():
    analysis = analyze_curve(_full_curve())
    assert analysis.peak_stress == 9.4
    assert analysis.peak_stress_time == "20:50"


def test_curve_analysis_normalizes_warning_and_calendar_times_to_local_timezone():
    analysis = analyze_curve(
        _full_curve(),
        warning_windows=[{
            "target_time": "2030-01-15T09:00:00+00:00",
            "risk_time": "2030-01-15T09:20:00+00:00",
        }],
        calendar_events=[{
            "summary": "晚间复盘",
            "start_time": "2030-01-15T10:00:00+00:00",
        }],
        timezone_value=ZoneInfo("Asia/Shanghai"),
    )
    assert analysis.warning_windows[0]["target_time_local"] == "17:00"
    assert analysis.warning_windows[0]["risk_time_local"] == "17:20"
    assert analysis.important_calendar_events[0]["time"] == "18:00"


def test_card_summary_matches_curve_analysis_peak():
    analysis = analyze_curve(_full_curve())
    card = pressure_curve_card(analysis, image_key="img", local_date="2030-01-15")
    markdown = "\n".join(
        item.get("content", "") for item in card["body"]["elements"]
        if item.get("tag") == "markdown"
    )
    assert "9.4/10" in markdown
    assert "20:50" in markdown


def test_renderer_and_card_use_same_curve_analysis():
    curve = _full_curve()
    analysis = analyze_curve(curve)
    png = PressureCurveRenderer().render(curve, analysis)
    template = pressure_curve_card(
        analysis,
        image_key=IMAGE_KEY_PLACEHOLDER,
        local_date="2030-01-15",
    )
    pending = PendingImageCard(png, template)
    materialized = pending.materialize("uploaded-key")
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert materialized["body"]["elements"][0]["img_key"] == "uploaded-key"
    assert analysis.peak_stress_time in materialized["body"]["elements"][1]["content"]


def test_renderer_matches_m0_state_definition_and_reference_visual_style():
    renderer = PressureCurveRenderer()
    curve = _full_curve()
    analysis = analyze_curve(
        curve,
        warning_windows=[{
            "risk_time": "2030-01-15T12:00:00+08:00",
            "warning_level": "2",
            "payload": {"time": "12:00", "S": 78.0, "type": "高压预警"},
        }],
        calendar_events=[{
            "summary": "项目课程",
            "event_type": "course",
            "start_time": "2030-01-15T09:00:00+08:00",
            "end_time": "2030-01-15T10:30:00+08:00",
        }],
        timezone_value=ZoneInfo("Asia/Shanghai"),
    )
    model_output = {
        "model_family": "stress-ctssm.m0",
        "model_variant": "m0",
        "active_states": ["S"],
        "stress_baseline_0_10": 5.5,
        "stress_threshold_0_10": 7.4,
        "vitality_baseline_0_10": 7.2,
        "energy_critical_0_10": 2.5,
    }
    figure = renderer._draw_core_plot(curve, analysis, model_output)
    try:
        assert tuple(figure.get_size_inches()) == (14.0, 9.0)
        assert len(figure.axes) == 3  # pressure, M0 inputs, alert confidence
        pressure_axis, input_axis, confidence_axis = figure.axes
        assert pressure_axis.get_ylabel() == "心理压力 (S, 0-100)"
        assert input_axis.get_ylabel() == "M0 公式输入 (0-1)"
        assert confidence_axis.get_ylabel() == "预警置信度 (0-1)"
        assert figure._suptitle.get_text() == "M0 压力时变平衡模型（真实预测）"
        pressure_labels = pressure_axis.get_legend_handles_labels()[1]
        assert "真实 M0 压力 S(t)" in pressure_labels
        assert "瞬时平衡 S_eq(t)" in pressure_labels
        assert "用户平衡值 S*=55" in pressure_labels
        assert "关怀观察线=74" in pressure_labels
        assert "连轴转惩罚生效区" not in pressure_labels
        input_labels = input_axis.get_legend_handles_labels()[1]
        assert input_labels == ["事件压力 U(t)", "事前预期 A(t)", "事后残留 H(t)"]
        assert "认知精力 E(t)" not in input_labels
    finally:
        renderer._pyplot()[0].close(figure)


def test_real_m0_solver_output_is_the_renderer_source():
    calendar_event = {
        "id": "real-m0-event",
        "summary": "项目汇报",
        "description": "正式项目汇报",
        "event_type": "task",
        "task_type": "meeting",
        "start_time": "2030-01-15T09:00:00+08:00",
        "end_time": "2030-01-15T10:30:00+08:00",
    }
    output = AssessmentModel("Asia/Shanghai").predict(
        profile={},
        observations=[],
        calendar_events=[calendar_event],
        local_date="2030-01-15",
    ).to_dict()
    curve = list(output["trajectory"])
    analysis = analyze_curve(
        curve,
        calendar_events=[calendar_event],
        timezone_value=ZoneInfo("Asia/Shanghai"),
    )

    assert output["model_family"] == "stress-ctssm.m0"
    assert output["model_variant"] == "m0"
    assert output["active_states"] == ("S",)
    assert output["point_count"] == 288
    assert len({point["vitality_0_10"] for point in curve}) == 1
    assert all(point["continuous_load_penalty"] == 0.0 for point in curve)
    assert any(point["event_stress_input"] > 0.0 for point in curve)
    assert any(point["anticipatory_input"] > 0.0 for point in curve)
    assert any(point["post_event_input"] > 0.0 for point in curve)
    assert all("stress_equilibrium_0_10" in point for point in curve)

    png = PressureCurveRenderer().render(curve, analysis, output)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")


class _ImageEndpoint:
    def __init__(self, response):
        self.response = response
        self.calls = 0

    def create(self, _request):
        self.calls += 1
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _image_client(response):
    endpoint = _ImageEndpoint(response)
    client = FeishuClient.__new__(FeishuClient)
    client._client = SimpleNamespace(
        im=SimpleNamespace(v1=SimpleNamespace(image=endpoint))
    )
    return client, endpoint


def test_image_upload_success():
    response = SimpleNamespace(
        success=lambda: True,
        data=SimpleNamespace(image_key="img-key"),
    )
    client, endpoint = _image_client(response)
    assert client.upload_image(b"\x89PNG\r\n\x1a\ncontent") == "img-key"
    assert endpoint.calls == 1


def test_image_upload_failure():
    response = SimpleNamespace(
        success=lambda: False,
        code=230001,
        msg="forbidden",
    )
    client, _ = _image_client(response)
    with pytest.raises(FeishuSendError) as caught:
        client.upload_image(b"\x89PNG\r\n\x1a\ncontent")
    assert caught.value.retryable is False
    assert caught.value.operation == "upload_image"


def test_image_upload_missing_image_key():
    response = SimpleNamespace(success=lambda: True, data=SimpleNamespace(image_key=""))
    client, _ = _image_client(response)
    with pytest.raises(FeishuSendError, match="image_key"):
        client.upload_image(b"\x89PNG\r\n\x1a\ncontent")


def test_send_image_uses_image_message_type():
    client = FeishuClient.__new__(FeishuClient)
    seen = []
    client._send_message = lambda chat_id, kind, content: (
        seen.append((chat_id, kind, content)) or "om-image"
    )
    assert client.send_image("oc", "img-key") == "om-image"
    assert seen == [("oc", "image", {"image_key": "img-key"})]


def test_send_text_sets_sdk_message_uuid():
    requests = []

    class Messages:
        def create(self, request):
            requests.append(request)
            return SimpleNamespace(
                success=lambda: True,
                data=SimpleNamespace(message_id="om-warning"),
            )

    sdk_client = SimpleNamespace(
        im=SimpleNamespace(v1=SimpleNamespace(message=Messages()))
    )
    client = FeishuClient("app", "secret", sdk_client=sdk_client)

    assert client.send_text(
        "oc-chat", "warning", message_uuid="stable-warning-id",
    ) == "om-warning"
    assert requests[0].request_body.uuid == "stable-warning-id"


def test_worker_uploads_then_sends_materialized_image_card():
    worker = BotWorker.__new__(BotWorker)
    worker.max_retries = 0

    class Sender:
        def __init__(self):
            self.sent = []

        def upload_image(self, png):
            assert png.startswith(b"\x89PNG")
            return "img-key"

        def send_card(self, chat_id, card):
            self.sent.append((chat_id, card))
            return "om-card"

    worker.sender = Sender()
    pending = PendingImageCard(
        b"\x89PNG\r\n\x1a\ncontent",
        {"body": {"elements": [{"img_key": IMAGE_KEY_PLACEHOLDER}]}},
    )
    result = asyncio.run(worker._send_image_card("oc", pending))
    assert result == "om-card"
    assert worker.sender.sent[0][1]["body"]["elements"][0]["img_key"] == "img-key"


class _MutationCoordinator:
    def __init__(self):
        self.calls = []

    async def ensure_forecast(self, participant_id, target, reason, **kwargs):
        self.calls.append((participant_id, target, reason, kwargs))
        return {"warning_diff": {"rescheduled": 1}}


class _MutationCalendar:
    def __init__(self):
        self.old = {
            "id": "e1",
            "start_time": "2030-01-15T09:00:00+08:00",
            "end_time": "2030-01-15T10:00:00+08:00",
        }

    async def get_event(self, *_args):
        return dict(self.old)

    async def create_event(self, _participant_id, **kwargs):
        return {
            "id": "e1",
            "start_time": kwargs["start_time"].isoformat(),
            "end_time": kwargs["end_time"].isoformat(),
        }

    async def update_event(self, _participant_id, _event_id, **kwargs):
        return {
            "id": "e1",
            "start_time": kwargs["start_time"].isoformat(),
            "end_time": kwargs["end_time"].isoformat(),
        }

    async def delete_event(self, _participant_id, event_id):
        return {"id": event_id, "deleted": True}


def _mutation_tools():
    coordinator = _MutationCoordinator()
    tools = CareTools(
        None, None, None, None, _MutationCalendar(), None, "Asia/Shanghai",
        coordinator,
    )
    ctx = AgentContext(uuid.uuid4(), "P", "ou", "oc", "message", uuid.uuid4())
    return tools, coordinator, ctx


def test_calendar_create_triggers_forecast_refresh():
    tools, coordinator, ctx = _mutation_tools()
    result = asyncio.run(tools.create_calendar_event(ctx, {
        "summary": "复盘",
        "start_time": "2030-01-15T09:00:00+08:00",
        "end_time": "2030-01-15T10:00:00+08:00",
    }))
    assert result["forecast_refreshed_dates"] == ["2030-01-15"]
    assert coordinator.calls[0][2] == "calendar_create_event"
    assert coordinator.calls[0][3]["refresh_calendar"] is True


def test_calendar_update_triggers_forecast_refresh_and_handles_cross_date():
    tools, coordinator, ctx = _mutation_tools()
    result = asyncio.run(tools.update_calendar_event(ctx, {
        "event_id": "e1",
        "start_time": "2030-01-16T09:00:00+08:00",
        "end_time": "2030-01-16T10:00:00+08:00",
    }))
    assert result["forecast_refreshed_dates"] == ["2030-01-15", "2030-01-16"]
    assert {call[2] for call in coordinator.calls} == {"calendar_update_event"}


def test_calendar_delete_triggers_forecast_refresh():
    tools, coordinator, ctx = _mutation_tools()
    result = asyncio.run(tools.delete_calendar_event(ctx, {
        "event_id": "e1",
        "confirmed": True,
    }))
    assert result["forecast_refreshed_dates"] == ["2030-01-15"]
    assert coordinator.calls[0][2] == "calendar_delete_event"


def test_calendar_mutation_reschedules_warning():
    database, person, calendar, _semantics, _prediction, _warnings, coordinator = (
        build_pipeline([event()])
    )
    asyncio.run(coordinator.ensure_forecast(person.id, TEST_LOCAL_DATE, "prepare"))
    with database.session() as session:
        assert session.query(WarningSchedule).one().status == "pending"

    old_event = dict(calendar.events[0])

    async def get_event(_participant_id, _event_id):
        return old_event

    async def delete_event(_participant_id, event_id):
        calendar.events = []
        return {"id": event_id, "deleted": True}

    calendar.get_event = get_event
    calendar.delete_event = delete_event
    tools = CareTools(
        None, None, None, None, calendar, None, "Asia/Shanghai", coordinator
    )
    ctx = AgentContext(person.id, "P", "ou", "oc", "message", uuid.uuid4())
    result = asyncio.run(tools.delete_calendar_event(ctx, {
        "event_id": old_event["id"],
        "confirmed": True,
    }))
    assert result["forecast_refreshed_dates"] == ["2030-01-15"]
    with database.session() as session:
        assert session.query(WarningSchedule).one().status == "cancelled"
