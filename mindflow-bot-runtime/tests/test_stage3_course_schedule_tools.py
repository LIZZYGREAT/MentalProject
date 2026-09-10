import asyncio
from datetime import date, time
import uuid

from app.agent.context import AgentContext
from app.agent.tool_registry import ToolRegistry
from app.contracts.course_schedule import ScheduleVisionResult
from app.services.presentation_service import PresentationOutbox
from app.repositories_course_schedule import CourseScheduleImportRepository
from app.services.mutation_intent_verifier import MutationIntentDecision
from app.tools.course_schedule import CourseScheduleTools
from helpers import memory_database, participant


def _payload(*, duplicate=False, actual_times=True):
    course = {
        "course_name": "高等数学",
        "weekday": 1,
        "period_start": 1,
        "period_end": 2,
        "start_time": "08:00" if actual_times else None,
        "end_time": "09:35" if actual_times else None,
        "location": "A101",
        "teacher": None,
        "week_rule": {
            "start_week": 1,
            "end_week": 16,
            "odd_even": "all",
            "explicit_weeks": None,
        },
        "period_inference_source": "grid_position",
        "period_confidence": 0.9,
        "uncertain_fields": [],
    }
    courses = [course]
    if duplicate:
        courses.append({**course, "weekday": 3, "location": "A102"})
    return {
        "document_type": "course_schedule",
        "semester_label": "2026-2027-1",
        "institution": None,
        "courses": courses,
        "missing_context": ["semester_start_date"],
        "warnings": [],
    }


def _draft(repo, participant_id, *, duplicate=False, actual_times=True):
    return repo.create_draft(
        participant_id,
        source_message_id=uuid.uuid4().hex,
        source_image_hash="a" * 64,
        vision_model="vision-model",
        result=ScheduleVisionResult.from_dict(
            _payload(duplicate=duplicate, actual_times=actual_times)
        ),
        timezone_name="Asia/Shanghai",
        semester_start_date=date(2026, 9, 7),
    )


def _context(participant_id, run_id, text):
    return AgentContext(
        participant_id=participant_id,
        participant_code="P-STAGE3",
        open_id="open",
        chat_id="chat",
        message_id="message",
        agent_run_id=run_id,
        user_request_text=text,
    )


class _Verifier:
    def __init__(self, decision):
        self.decision = decision
        self.calls = []

    async def verify(self, **kwargs):
        self.calls.append(kwargs)
        return self.decision


class _Imports:
    def __init__(self, drafts):
        self.drafts = drafts

    def cancel(self, participant_id, import_id):
        draft = self.drafts.cancel(participant_id, import_id)
        return {
            "ok": draft["status"] == "cancelled",
            "status": draft["status"],
            "reply_text": "已取消这次课程表导入。",
        }

    def cancel_or_revert(self, participant_id, selector):
        candidate = self.drafts.resolve_cancel_selector(participant_id, selector)
        draft = self.drafts.request_cancel(participant_id, candidate["id"])
        return {
            "ok": True,
            "status": draft["status"],
            "cancel_mode": draft.get("cancel_mode"),
            "already_cancelled": bool(draft.get("already_cancelled")),
            "reply_text": "已清理这次课程表导入。",
        }


def test_repository_structured_correction_separates_selector_and_new_weekday():
    database = memory_database()
    owner = participant(database, "STAGE3-REPO")
    repo = CourseScheduleImportRepository(database)
    draft = _draft(repo, owner.id, duplicate=True)

    corrected = repo.apply_correction(
        owner.id,
        draft["id"],
        course_name="高等数学",
        selector_weekday=3,
        new_weekday=4,
        period_start=3,
        period_end=4,
        start_time="08:20",
        end_time="09:55",
        week_start=1,
        week_end=15,
        odd_even="odd",
        location="A201",
    )

    first, changed = corrected["structured_result"]["courses"]
    assert first["weekday"] == 1
    assert changed["weekday"] == 4
    assert (changed["period_start"], changed["period_end"]) == (3, 4)
    assert (changed["start_time"], changed["end_time"]) == ("08:20", "09:55")
    assert changed["week_rule"] == {
        "start_week": 1,
        "end_week": 15,
        "odd_even": "odd",
        "explicit_weeks": None,
    }
    assert changed["location"] == "A201"
    assert corrected["structured_result"]["_metadata"]["course_time_sources"][1] == "user_actual"


def test_participant_bound_tools_read_update_and_stage_preview_without_calendar_write():
    database = memory_database()
    owner = participant(database, "STAGE3-TOOLS")
    repo = CourseScheduleImportRepository(database)
    _draft(repo, owner.id, actual_times=False)
    presentations = PresentationOutbox()
    verifier = _Verifier(
        MutationIntentDecision("allow", "direct_action", "direct_request")
    )
    registry = ToolRegistry(mutation_verifier=verifier)
    CourseScheduleTools(_Imports(repo), presentations).register(registry)
    run_id = uuid.uuid4()
    ctx = _context(owner.id, run_id, "高数改成周四第3-4节，1-15周单周，地点A101")

    async def scenario():
        before = await registry.execute(ctx, "course_schedule_get_active_draft", {})
        updated = await registry.execute(
            ctx,
            "course_schedule_update_active_draft",
            {
                "selector": {"course_name": "高等数学"},
                "updates": {
                    "new_weekday": 4,
                    "period_start": 3,
                    "period_end": 4,
                    "week_start": 1,
                    "week_end": 15,
                    "odd_even": "odd",
                    "location": "A101",
                },
            },
        )
        return before, updated

    before, updated = asyncio.run(scenario())
    assert before.result["ok"] is True
    assert updated.result["ok"] is True
    course = updated.result["draft"]["courses"][0]
    assert (course["weekday"], course["period_start"], course["period_end"]) == (4, 3, 4)
    assert course["time_source"] == "default"
    cards = presentations.take_cards(run_id)
    assert len(cards) == 1
    assert "学校默认作息" in str(cards[0])
    assert len(verifier.calls) == 1
    assert "participant" not in str(verifier.calls[0]["proposal_summary"]).lower()


def test_hypothetical_correction_is_denied_before_draft_mutation():
    database = memory_database()
    owner = participant(database, "STAGE3-HYPOTHETICAL")
    repo = CourseScheduleImportRepository(database)
    original = _draft(repo, owner.id)
    presentations = PresentationOutbox()
    verifier = _Verifier(
        MutationIntentDecision("deny", "hypothetical", "hypothetical")
    )
    registry = ToolRegistry(mutation_verifier=verifier)
    CourseScheduleTools(_Imports(repo), presentations).register(registry)
    ctx = _context(owner.id, uuid.uuid4(), "高数改成第3-4节会冲突吗？")

    result = asyncio.run(
        registry.execute(
            ctx,
            "course_schedule_update_active_draft",
            {
                "selector": {"course_name": "高等数学"},
                "updates": {"period_start": 3, "period_end": 4},
            },
        )
    )

    assert result.status == "tool_effect_not_authorized"
    unchanged = repo.get(original["id"])["structured_result"]["courses"][0]
    assert (unchanged["period_start"], unchanged["period_end"]) == (1, 2)
    assert presentations.take_cards(ctx.agent_run_id) == []


def test_participant_bound_pending_cancel_without_provider_effect_skips_verifier():
    database = memory_database()
    owner = participant(database, "STAGE3-CANCEL-BOUND")
    repo = CourseScheduleImportRepository(database)
    _draft(repo, owner.id)
    verifier = _Verifier(None)
    registry = ToolRegistry(mutation_verifier=verifier)
    CourseScheduleTools(_Imports(repo), PresentationOutbox()).register(registry)
    ctx = _context(owner.id, uuid.uuid4(), "清理这次尚未导入的课表")

    result = asyncio.run(
        registry.execute(ctx, "course_schedule_cancel_pending_draft", {})
    )

    assert result.status == "succeeded"
    assert result.result["status"] == "cancelled"
    assert verifier.calls == []


def test_cancel_with_provider_effect_still_requires_semantic_verification():
    class Drafts:
        def resolve_cancel_selector(self, participant_id, selector):
            assert participant_id == owner.id
            assert selector == {"latest": True}
            return {
                "id": "private-import-id",
                "status": "succeeded",
                "created_at": "2026-09-10T08:00:00+00:00",
                "created_local_date": "2026-09-10",
                "course_names": ["高等数学"],
                "has_provider_effect": True,
            }

    class Imports:
        drafts = Drafts()

        def cancel_or_revert(self, participant_id, selector):
            assert participant_id == owner.id
            return {"ok": True, "status": "cancelling", "cancel_mode": "revert"}

    database = memory_database()
    owner = participant(database, "STAGE3-REVERT-VERIFY")
    verifier = _Verifier(
        MutationIntentDecision("allow", "cancel_or_revert", "explicit_cleanup")
    )
    registry = ToolRegistry(mutation_verifier=verifier)
    CourseScheduleTools(Imports(), PresentationOutbox()).register(registry)
    ctx = _context(owner.id, uuid.uuid4(), "撤销刚才导入到日历的课程")

    result = asyncio.run(
        registry.execute(
            ctx,
            "course_schedule_cancel_or_revert_import",
            {"selector": {"latest": True}},
        )
    )

    assert result.status == "succeeded"
    assert result.result["status"] == "cancelling"
    assert len(verifier.calls) == 1
    serialized = str(verifier.calls[0]["proposal_summary"])
    assert "private-import-id" not in serialized


def test_context_tool_applies_user_period_mapping_and_semester_monday():
    database = memory_database()
    owner = participant(database, "STAGE3-CONTEXT")
    repo = CourseScheduleImportRepository(database)
    _draft(repo, owner.id, actual_times=False)
    verifier = _Verifier(
        MutationIntentDecision("allow", "direct_action", "direct_request")
    )
    registry = ToolRegistry(mutation_verifier=verifier)
    CourseScheduleTools(_Imports(repo), PresentationOutbox()).register(registry)
    ctx = _context(owner.id, uuid.uuid4(), "第一周周一是2026-09-07，第1-2节08:10-09:50")

    result = asyncio.run(
        registry.execute(
            ctx,
            "course_schedule_update_active_context",
            {
                "semester_start_date": "2026-09-07",
                "period_time_mapping": [
                    {
                        "period_start": 1,
                        "period_end": 2,
                        "start_time": "08:10",
                        "end_time": "09:50",
                    }
                ],
            },
        )
    )

    assert result.result["ok"] is True
    assert result.result["draft"]["status"] == "pending_confirmation"
    course = result.result["draft"]["courses"][0]
    assert (course["start_time"], course["end_time"], course["time_source"]) == (
        "08:10",
        "09:50",
        "user",
    )


def test_context_tool_is_atomic_when_mapping_validation_fails():
    database = memory_database()
    owner = participant(database, "STAGE3-ATOMIC")
    repo = CourseScheduleImportRepository(database)
    draft = _draft(repo, owner.id, actual_times=False)
    verifier = _Verifier(
        MutationIntentDecision("allow", "direct_action", "direct_request")
    )
    registry = ToolRegistry(mutation_verifier=verifier)
    CourseScheduleTools(_Imports(repo), PresentationOutbox()).register(registry)
    ctx = _context(owner.id, uuid.uuid4(), "补充课表作息")

    result = asyncio.run(
        registry.execute(
            ctx,
            "course_schedule_update_active_context",
            {
                "semester_start_date": "2026-09-14",
                "period_time_mapping": [
                    {
                        "period_start": 1,
                        "period_end": 2,
                        "start_time": "10:00",
                        "end_time": "09:00",
                    }
                ],
            },
        )
    )

    assert result.result["ok"] is False
    unchanged = repo.get(draft["id"])
    assert unchanged["semester_start_date"] == "2026-09-07"
    assert unchanged["structured_result"]["_metadata"].get(
        "user_period_mapping"
    ) in (None, {})


def _period_mapping(start="08:10", end="09:50"):
    return {(1, 2): (time.fromisoformat(start), time.fromisoformat(end))}


def test_user_actual_time_survives_semester_only_context_update():
    database = memory_database()
    owner = participant(database, "STAGE3-ACTUAL-SEMESTER")
    repo = CourseScheduleImportRepository(database)
    draft = _draft(repo, owner.id, actual_times=False)
    corrected = repo.apply_correction(
        owner.id,
        draft["id"],
        course_name="高等数学",
        start_time="08:20",
        end_time="09:55",
    )

    updated = repo.apply_context_update(
        owner.id, corrected["id"], semester_start_date=date(2026, 9, 7)
    )
    course = updated["structured_result"]["courses"][0]
    assert (course["start_time"], course["end_time"]) == ("08:20", "09:55")
    assert updated["structured_result"]["_metadata"]["course_time_sources"][0] == (
        "user_actual"
    )


def test_user_actual_time_survives_later_period_mapping():
    database = memory_database()
    owner = participant(database, "STAGE3-ACTUAL-MAPPING")
    repo = CourseScheduleImportRepository(database)
    draft = _draft(repo, owner.id, actual_times=False)
    corrected = repo.apply_correction(
        owner.id,
        draft["id"],
        course_name="高等数学",
        start_time="08:20",
        end_time="09:55",
    )

    updated = repo.apply_context_update(
        owner.id,
        corrected["id"],
        period_time_mapping=_period_mapping(),
    )
    course = updated["structured_result"]["courses"][0]
    assert (course["start_time"], course["end_time"]) == ("08:20", "09:55")
    assert updated["structured_result"]["_metadata"]["course_time_sources"][0] == (
        "user_actual"
    )


def test_default_time_can_be_overridden_by_user_period_mapping():
    database = memory_database()
    owner = participant(database, "STAGE3-DEFAULT-MAPPING")
    repo = CourseScheduleImportRepository(database)
    draft = _draft(repo, owner.id, actual_times=False)

    updated = repo.apply_context_update(
        owner.id, draft["id"], period_time_mapping=_period_mapping()
    )
    course = updated["structured_result"]["courses"][0]
    assert (course["start_time"], course["end_time"]) == ("08:10", "09:50")
    assert updated["structured_result"]["_metadata"]["course_time_sources"][0] == (
        "user"
    )


def test_image_actual_time_remains_authoritative_over_period_mapping():
    database = memory_database()
    owner = participant(database, "STAGE3-IMAGE-MAPPING")
    repo = CourseScheduleImportRepository(database)
    draft = _draft(repo, owner.id, actual_times=True)

    updated = repo.apply_context_update(
        owner.id, draft["id"], period_time_mapping=_period_mapping()
    )
    course = updated["structured_result"]["courses"][0]
    assert (course["start_time"], course["end_time"]) == ("08:00", "09:35")
    assert updated["structured_result"]["_metadata"]["course_time_sources"][0] == (
        "image"
    )
