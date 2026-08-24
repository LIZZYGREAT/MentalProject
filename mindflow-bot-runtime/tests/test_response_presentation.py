import asyncio
from datetime import datetime, timezone
from pathlib import Path
import time

import pytest

from app.integrations.feishu.client import FeishuSendError
from app.integrations.feishu.gateway import BotEvent
from app.presentation.contracts import AgentActivityEvent, ResponsePlan, ResponseSegment, RuntimeResponse
from app.presentation.markdown_sanitizer import MarkdownSanitizer
from app.presentation.progress_presenter import ProgressPresenter
from app.presentation.presentation_agent import ProductionPresentationAgent
from app.presentation.response_orchestrator import ResponseOrchestrator
from app.presentation.semantic_segmenter import SemanticSegmenter
from app.repositories import BotEventRepository
from app.models import BotEvent as BotEventRow
from app.worker import BotWorker, ProgressState
from helpers import memory_database


def test_dockerfile_pins_debian_apt_to_aliyun_without_overwriting_custom_sources():
    dockerfile = (
        Path(__file__).resolve().parents[1] / "Dockerfile"
    ).read_text(encoding="utf-8")

    mirror_position = dockerfile.index("mirrors.aliyun.com/debian")
    update_position = dockerfile.index("apt-get update")
    assert mirror_position < update_position
    assert "/etc/apt/sources.list.d/debian.sources" in dockerfile
    assert "/etc/apt/sources.list" in dockerfile
    assert "grep -q 'deb.debian.org'" in dockerfile
    assert "mirrors.aliyun.com/debian-security" in dockerfile


def test_markdown_sanitizer_removes_presentation_tokens_conservatively():
    source = (
        "## 关键节点\n"
        "**今日压力**\n"
        "- 15:45 压力上升\n"
        "- `stress=8`\n"
        "[飞书帮助](https://example.com/help)\n"
        "公式 a*b、name_value、-12.5% 保持"
    )
    result = MarkdownSanitizer().sanitize(source)

    assert "##" not in result
    assert "**" not in result
    assert "```" not in result
    assert "• 15:45" in result
    assert "stress=8" in result
    assert "飞书帮助：https://example.com/help" in result
    assert "a*b" in result
    assert "name_value" in result
    assert "-12.5%" in result


def test_semantic_segmenter_keeps_short_answers_single_and_long_answers_bounded():
    segmenter = SemanticSegmenter(
        min_total_chars=80, target_chars=55, max_chars=100, max_segments=3
    )
    assert segmenter.segment("已经创建好了。") == ("已经创建好了。",)

    text = (
        "先说结论：今天整体压力处于中等偏高水平。"
        "主要影响来自连续的高强度任务，15:45 左右进入较高压力区间。"
        "详情见 https://example.com/report/2026-08-23 ，这个地址不能从中间断开。"
        "如果可以，建议在两段任务之间留一点缓冲。"
    )
    segments = segmenter.segment(text)

    assert 2 <= len(segments) <= 3
    assert all(segment and len(segment) <= 100 for segment in segments)
    assert "15:45" in "".join(segments)
    assert "https://example.com/report/2026-08-23" in "".join(segments)


def test_semantic_segmenter_never_drops_authoritative_tail():
    segmenter = SemanticSegmenter(
        min_total_chars=320,
        target_chars=260,
        max_chars=650,
        max_segments=3,
    )
    source = "前段分析内容。" * 430 + "最终风险时间 15:45，压力值 74.06。"

    segments = segmenter.segment(source)
    combined = "".join(segments)

    assert len(source) > 3 * 650
    assert segments == (source,)
    assert combined == source
    assert "15:45" in combined
    assert "74.06" in combined


def test_progress_presenter_uses_task_stage_and_avoids_generic_after_activity():
    presenter = ProgressPresenter()
    state = ProgressState()
    first = presenter.present(
        AgentActivityEvent(kind="tool_started", tool_name="calendar_list_events"),
        state=state,
    )
    second = presenter.present(
        AgentActivityEvent(kind="tool_started", tool_name="care_get_pressure_curve"),
        state=state,
    )

    assert first == "我先看看相关日程。"
    assert second == "日程信息拿到了，我正在结合这些安排计算压力变化。"
    assert presenter.delayed(state=state) is None
    assert "正在处理，请稍候" not in first + second


class GoodPresentationAgent:
    def __init__(self):
        self.calls = 0

    async def compose(self, *_args, **_kwargs):
        self.calls += 1
        return ("先说结论：峰值在 15:45，数值为 74.06。", "接下来可以留出缓冲。")


class BadNumericPresentationAgent:
    async def compose(self, *_args, **_kwargs):
        return ("峰值改成了 16:00，数值为 80。",)


class TimeoutPresentationAgent:
    async def compose(self, *_args, **_kwargs):
        await asyncio.sleep(0.05)
        return ("不会采用",)


class MarkdownPresentationAgent:
    async def compose(self, *_args, **_kwargs):
        return (
            "- 第一项：15:45 压力值 74.06",
            "`inline` [帮助](https://example.com)",
        )


def test_response_orchestrator_routes_only_long_analysis_to_presentation_agent():
    agent = GoodPresentationAgent()
    orchestrator = ResponseOrchestrator(
        presentation_agent=agent,
        presentation_agent_mode="always",
        presentation_agent_min_chars=10,
        segmenter=SemanticSegmenter(min_total_chars=10, max_chars=650),
    )
    source = RuntimeResponse(
        "下午的分析比较长，峰值在 15:45，数值为 74.06。",
        response_kind="analysis",
    )
    plan = asyncio.run(orchestrator.build_plan(source, cards=[], used_tools=set()))

    assert plan.presentation_agent_used is True
    assert plan.presentation_agent_attempted is True
    assert plan.presentation_agent_outcome == "used"
    assert len(plan.segments) == 2
    assert agent.calls == 1

    transactional = asyncio.run(
        orchestrator.build_plan(
            RuntimeResponse("**已经创建成功**"),
            cards=[],
            used_tools={"calendar_create_event"},
        )
    )
    assert transactional.kind == "transactional"
    assert transactional.presentation_agent_used is False
    assert transactional.segments[0].text == "已经创建成功"
    assert agent.calls == 1


def test_pressure_curve_card_companion_is_formal_and_contains_no_extra_analysis():
    plan = asyncio.run(
        ResponseOrchestrator().build_plan(
            RuntimeResponse(
                "内部工具返回了很多绘图参数、模型版本和诊断信息。",
                response_kind="analysis",
            ),
            cards=[{"schema": "2.0"}],
            used_tools={"care_get_pressure_curve"},
        )
    )

    assert plan.kind == "rich"
    assert plan.full_text == "今日压力曲线已生成，请查看卡片。"
    assert len(plan.segments) == 1
    assert "绘图参数" not in plan.full_text
    assert "模型版本" not in plan.full_text
    assert "诊断信息" not in plan.full_text

def test_presentation_agent_validation_and_timeout_fall_back_deterministically():
    source = RuntimeResponse(
        "**分析**：峰值在 15:45，数值为 74.06。后续建议保持缓冲。",
        response_kind="analysis",
    )
    bad = ResponseOrchestrator(
        presentation_agent=BadNumericPresentationAgent(),
        presentation_agent_mode="always",
        presentation_agent_min_chars=1,
        segmenter=SemanticSegmenter(min_total_chars=1),
    )
    bad_plan = asyncio.run(bad.build_plan(source, cards=[], used_tools=set()))
    assert bad_plan.presentation_agent_used is False
    assert bad_plan.presentation_agent_attempted is True
    assert bad_plan.presentation_agent_outcome == "validation_reject"
    assert "15:45" in bad_plan.full_text
    assert "74.06" in bad_plan.full_text
    assert "**" not in bad_plan.full_text

    slow = ResponseOrchestrator(
        presentation_agent=TimeoutPresentationAgent(),
        presentation_agent_mode="always",
        presentation_agent_min_chars=1,
        presentation_agent_timeout_seconds=0.01,
        segmenter=SemanticSegmenter(min_total_chars=1),
    )
    slow_plan = asyncio.run(slow.build_plan(source, cards=[], used_tools=set()))
    assert slow_plan.presentation_agent_used is False
    assert slow_plan.presentation_agent_outcome == "timeout"
    assert slow_plan.presentation_agent_latency_ms < 40
    assert "15:45" in slow_plan.full_text


def test_presentation_agent_output_always_passes_through_markdown_sanitizer():
    orchestrator = ResponseOrchestrator(
        presentation_agent=MarkdownPresentationAgent(),
        presentation_agent_mode="always",
        presentation_agent_min_chars=1,
        segmenter=SemanticSegmenter(min_total_chars=1),
    )
    plan = asyncio.run(
        orchestrator.build_plan(
            RuntimeResponse(
                "第一项：15:45 压力值 74.06。inline 帮助内容。",
                response_kind="analysis",
            ),
            cards=[],
            used_tools=set(),
        )
    )

    assert plan.presentation_agent_used is True
    assert tuple(segment.text for segment in plan.segments) == (
        "• 第一项：15:45 压力值 74.06",
        "inline 帮助：https://example.com",
    )
    assert "`" not in plan.full_text
    assert "[帮助](" not in plan.full_text
    assert "- 第一项" not in plan.full_text


def test_invalid_presentation_fallback_keeps_complete_authoritative_answer():
    source = (
        "**前段分析**。" * 500
        + "最终风险时间 15:45，压力值 74.06。"
    )
    orchestrator = ResponseOrchestrator(
        presentation_agent=BadNumericPresentationAgent(),
        presentation_agent_mode="always",
        presentation_agent_min_chars=1,
    )
    plan = asyncio.run(
        orchestrator.build_plan(
            RuntimeResponse(source, response_kind="analysis"),
            cards=[],
            used_tools=set(),
        )
    )

    expected = MarkdownSanitizer().sanitize(source)
    assert plan.presentation_agent_used is False
    assert len(plan.segments) == 1
    assert plan.segments[0].text == expected
    assert "15:45" in plan.full_text
    assert "74.06" in plan.full_text


def test_adaptive_mode_skips_secondary_model_when_local_plan_is_lossless_and_bounded():
    agent = GoodPresentationAgent()
    source = (
        "今天的分析会完整保留。15:45 的数值是 74.06。"
        "建议在连续任务之间留出十分钟缓冲。"
    ) * 12
    orchestrator = ResponseOrchestrator(
        presentation_agent=agent,
        presentation_agent_mode="adaptive",
        presentation_agent_min_chars=100,
        segmenter=SemanticSegmenter(
            min_total_chars=100, target_chars=260, max_chars=650, max_segments=3
        ),
    )

    plan = asyncio.run(orchestrator.build_plan(
        RuntimeResponse(source, response_kind="analysis"),
        cards=[], used_tools=set(),
    ))

    assert agent.calls == 0
    assert plan.presentation_agent_attempted is False
    assert plan.presentation_agent_outcome == "skipped_adaptive"
    assert plan.presentation_agent_latency_ms == 0
    assert 1 <= len(plan.segments) <= 3
    assert all(len(segment.text) <= 650 for segment in plan.segments)
    assert "15:45" in plan.full_text and "74.06" in plan.full_text


def test_timeout_is_a_hard_user_visible_deadline_and_cleanup_applies_backpressure():
    class SlowCancellationAgent:
        async def compose(self, *_args, **_kwargs):
            try:
                await asyncio.sleep(10)
            finally:
                await asyncio.sleep(0.3)

    async def scenario():
        orchestrator = ResponseOrchestrator(
            presentation_agent=SlowCancellationAgent(),
            presentation_agent_mode="always",
            presentation_agent_min_chars=1,
            presentation_agent_timeout_seconds=0.01,
            presentation_agent_max_pending_cleanups=1,
            segmenter=SemanticSegmenter(min_total_chars=1),
        )
        source = RuntimeResponse(
            "完整权威结论：15:45 的数值是 74.06。", response_kind="analysis"
        )
        started = time.monotonic()
        first = await orchestrator.build_plan(source, cards=[], used_tools=set())
        elapsed = time.monotonic() - started
        second = await orchestrator.build_plan(source, cards=[], used_tools=set())
        await asyncio.sleep(0.35)
        await orchestrator.close()
        return first, second, elapsed

    first, second, elapsed = asyncio.run(scenario())

    assert elapsed < 0.18
    assert first.presentation_agent_outcome == "timeout"
    assert first.presentation_cleanup_pending == 1
    assert "15:45" in first.full_text and "74.06" in first.full_text
    assert second.presentation_agent_attempted is False
    assert second.presentation_agent_outcome == "cleanup_backpressure"


def test_safety_locked_response_is_byte_for_byte_and_never_rewritten():
    original = "  固定支持回复。\n请联系可信任的人。  "
    plan = asyncio.run(
        ResponseOrchestrator(
            presentation_agent=BadNumericPresentationAgent(),
            presentation_agent_min_chars=1,
        ).build_plan(
            RuntimeResponse(original, safety_locked=True, response_kind="fixed"),
            cards=[],
            used_tools={"care_run_today_assessment"},
        )
    )
    assert plan.full_text == original
    assert plan.segments[0].text == original
    assert plan.presentation_agent_used is False


def test_production_presentation_agent_is_stateless_toolless_and_skillless(monkeypatch, tmp_path):
    class SDK:
        last_client = None

        class ClaudeAgentOptions:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        class ResultMessage:
            def __init__(self):
                self.is_error = False
                self.result = '{"segments":["整理后的回复"]}'

        class ClaudeSDKClient:
            def __init__(self, options):
                self.options = options
                self.prompt = None
                SDK.last_client = self

            async def connect(self):
                return None

            async def query(self, prompt):
                self.prompt = prompt

            async def receive_response(self):
                yield SDK.ResultMessage()

            async def disconnect(self):
                return None

    monkeypatch.setattr("app.presentation.presentation_agent._load_sdk", lambda: SDK)
    agent = ProductionPresentationAgent(
        workdir=tmp_path,
        model="flash",
        base_url="https://model.example",
        auth_token="secret",
        opus_model="pro",
        sonnet_model="pro",
        haiku_model="flash",
    )
    result = asyncio.run(
        agent.compose(
            "authoritative",
            response_kind="analysis",
            has_card=False,
            max_segments=3,
        )
    )

    assert result == ("整理后的回复",)
    options = SDK.last_client.options
    assert options.tools == []
    assert options.skills == []
    assert options.allowed_tools == []
    assert options.plugins == []
    assert options.max_turns == 1
    assert "mcp_servers" not in options.__dict__
    assert "Skill" in options.disallowed_tools
    assert "participant_id" not in SDK.last_client.prompt


def test_production_presentation_agent_disconnect_has_an_independent_deadline(
    monkeypatch, tmp_path
):
    class SDK:
        class ClaudeAgentOptions:
            def __init__(self, **kwargs): self.__dict__.update(kwargs)
        class ResultMessage:
            is_error = False
            result = '{"segments":["完整结果 15:45 / 74.06"]}'
        class ClaudeSDKClient:
            def __init__(self, options): self.options = options
            async def connect(self): return None
            async def query(self, _prompt): return None
            async def receive_response(self): yield SDK.ResultMessage()
            async def disconnect(self): await asyncio.sleep(0.3)

    monkeypatch.setattr("app.presentation.presentation_agent._load_sdk", lambda: SDK)
    agent = ProductionPresentationAgent(
        workdir=tmp_path, model="flash", base_url="https://model.example",
        auth_token="secret", opus_model="pro", sonnet_model="pro",
        haiku_model="flash", disconnect_timeout_seconds=0.01,
    )

    started = time.monotonic()
    result = asyncio.run(agent.compose(
        "完整结果 15:45 / 74.06", response_kind="analysis",
        has_card=False, max_segments=3,
    ))
    elapsed = time.monotonic() - started

    assert result == ("完整结果 15:45 / 74.06",)
    assert elapsed < 0.15


class ProviderSender:
    def __init__(self, *, fail_text=None):
        self.calls = []
        self.visible = []
        self.by_uuid = {}
        self.fail_text = fail_text

    def send_text(self, chat_id, text, *, message_uuid=None):
        self.calls.append((chat_id, text, message_uuid))
        if text == self.fail_text:
            raise FeishuSendError("planned failure", retryable=False)
        if message_uuid in self.by_uuid:
            return self.by_uuid[message_uuid]
        message_id = f"out-{len(self.by_uuid) + 1}"
        self.by_uuid[message_uuid] = message_id
        self.visible.append((chat_id, text, message_uuid))
        return message_id


def _event_and_repository(event_id="delivery-1"):
    database = memory_database()
    repository = BotEventRepository(database)
    now = datetime.now(timezone.utc)
    repository.accept(
        event_id,
        f"message-{event_id}",
        None,
        app_id="app",
        open_id="open",
        chat_id="chat",
        chat_type="p2p",
        text="request",
        create_time=now,
    )
    repository.set_processing(event_id, None)
    return repository, BotEvent(
        event_id,
        f"message-{event_id}",
        "app",
        "open",
        "chat",
        "request",
        now,
    )


def _worker(repository, sender):
    return BotWorker(
        asyncio.Queue(),
        None,
        repository,
        None,
        None,
        None,
        sender,
        model="test",
        max_retries=0,
    )


def _three_segment_plan():
    return ResponsePlan(
        kind="analysis",
        full_text="A\n\nB\n\nC",
        segments=(
            ResponseSegment(0, "A"),
            ResponseSegment(1, "B"),
            ResponseSegment(2, "C"),
        ),
        use_cards=False,
    )


def test_crash_after_send_reuses_provider_uuid_and_does_not_duplicate_visible_segment():
    repository, event = _event_and_repository("crash-send")
    sender = ProviderSender()
    worker = _worker(repository, sender)
    original = repository.mark_reply_segment_sent
    crashed = False

    def crash_once(*args, **kwargs):
        nonlocal crashed
        if not crashed:
            crashed = True
            raise RuntimeError("crash after provider accepted A")
        return original(*args, **kwargs)

    repository.mark_reply_segment_sent = crash_once
    with pytest.raises(RuntimeError):
        asyncio.run(worker._deliver_plan(event, _three_segment_plan()))
    assert repository.pending_reply_plan(event.event_id).next_segment == 0

    repository.mark_reply_segment_sent = original
    asyncio.run(_worker(repository, sender)._deliver_plan(event, _three_segment_plan()))
    assert [text for _chat, text, _uuid in sender.visible] == ["A", "B", "C"]
    assert sender.calls[0][2] == sender.calls[1][2]


def test_resume_after_db_advance_starts_from_next_segment():
    repository, event = _event_and_repository("crash-db")
    sender = ProviderSender()
    plan = _three_segment_plan()
    repository.stage_reply_plan(
        event.event_id,
        full_text=plan.full_text,
        segments=[segment.text for segment in plan.segments],
    )
    first_uuid = BotWorker._stable_message_uuid(
        f"mindflow:reply:{event.event_id}:0"
    )
    first_id = sender.send_text("chat", "A", message_uuid=first_uuid)
    repository.mark_reply_segment_sent(
        event.event_id, segment_index=0, message_id=first_id
    )

    pending = repository.pending_reply_plan(event.event_id)
    assert pending.next_segment == 1
    asyncio.run(_worker(repository, sender)._resume_delivery_plan(event, pending))
    assert [text for _chat, text, _uuid in sender.visible] == ["A", "B", "C"]


def test_second_segment_failure_persists_next_index_and_resume_sends_only_remaining():
    repository, event = _event_and_repository("fail-b")
    first_sender = ProviderSender(fail_text="B")
    delivered = asyncio.run(
        _worker(repository, first_sender)._deliver_plan(event, _three_segment_plan())
    )
    assert delivered is False
    assert repository.pending_reply_plan(event.event_id).next_segment == 1

    second_sender = ProviderSender()
    pending = repository.pending_reply_plan(event.event_id)
    asyncio.run(_worker(repository, second_sender)._resume_delivery_plan(event, pending))
    assert [text for _chat, text, _uuid in second_sender.visible] == ["B", "C"]


def test_legacy_single_reply_recovers_as_one_segment_plan(caplog):
    repository, event = _event_and_repository("legacy")
    with repository.database.session() as session:
        row = session.get(BotEventRow, event.event_id)
        row.reply_text = "hello"
        row.reply_segments_json = None
        row.reply_next_segment = 0
        row.reply_message_ids_json = None
        row.reply_plan_version = None
        row.status = "reply_pending"
    with caplog.at_level("WARNING"):
        pending = repository.pending_reply_plan(event.event_id)
    assert pending.segments == ("hello",)
    assert pending.plan_version == "legacy-single-v1"
    assert "legacy_reply_plan_recovered" in caplog.messages

    sender = ProviderSender()
    asyncio.run(_worker(repository, sender)._resume_delivery_plan(event, pending))
    assert [text for _chat, text, _uuid in sender.visible] == ["hello"]


def test_presentation_outcome_metrics_are_persisted_without_unapproved_fields():
    repository, event = _event_and_repository("presentation-telemetry")
    repository.save_telemetry(event.event_id, {
        "presentation_ms": 12.3,
        "presentation_agent_used": False,
        "presentation_agent_attempted": True,
        "presentation_agent_outcome": "timeout",
        "presentation_agent_latency_ms": 10.1,
        "presentation_cleanup_pending": 1,
        "provider_secret": "must-not-persist",
    })

    with repository.database.session() as session:
        telemetry = dict(session.get(BotEventRow, event.event_id).telemetry_json)

    assert telemetry == {
        "presentation_ms": 12.3,
        "presentation_agent_used": False,
        "presentation_agent_attempted": True,
        "presentation_agent_outcome": "timeout",
        "presentation_agent_latency_ms": 10.1,
        "presentation_cleanup_pending": 1,
    }
