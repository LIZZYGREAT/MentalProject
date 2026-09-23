"""Deterministic Stage 1 scenario-bank construction.

The generator emits observable facts and hidden design metadata into physically
separate files. It never creates annotations or Gold labels.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any, Iterable


TZ = timezone(timedelta(hours=8))
CALIBRATION_START = datetime(2026, 9, 7, 8, 0, tzinfo=TZ)


@dataclass(frozen=True)
class ScenarioArtifact:
    visible: dict[str, Any]
    coverage: dict[str, Any]
    anchor: dict[str, Any] | None = None


def _iso(value: datetime) -> str:
    return value.isoformat()


def _course(course_ref: str, title: str, weekdays: list[int], start: str, end: str, known_at: datetime) -> dict[str, Any]:
    return {
        "course_ref": course_ref,
        "title": title,
        "weekdays": weekdays,
        "start_time": start,
        "end_time": end,
        "week2_same_as_week1": True,
        "known_at": _iso(known_at),
        "source_ref": f"calendar:{course_ref}",
    }


def _pack(pack_number: int) -> dict[str, Any]:
    pack_id = f"PACK_{pack_number:02d}"
    participant_id = f"P{pack_number:02d}"
    known_at = CALIBRATION_START - timedelta(days=1)
    if pack_number == 1:
        load = "低课程负荷；每周 4 个 recurring course blocks"
        courses = [
            _course("P01_MATH", "高等数学", [2], "10:00", "11:40", known_at),
            _course("P01_ENGLISH", "大学英语", [4], "14:00", "15:40", known_at),
        ]
    elif pack_number == 2:
        load = "低课程负荷；周末无 recurring course"
        courses = [
            _course("P02_STATS", "统计学", [1, 4], "08:00", "09:40", known_at),
            _course("P02_LAB", "计算机实验", [3], "14:00", "15:40", known_at),
        ]
    elif pack_number == 3:
        load = "中等课程负荷；课程外有研究任务"
        courses = [
            _course("P03_METHOD", "研究方法", [1, 3], "10:00", "11:40", known_at),
            _course("P03_SEMINAR", "专业研讨", [5], "14:00", "15:40", known_at),
            _course("P03_PE", "体育", [2], "16:00", "17:40", known_at),
        ]
    else:
        load = "高密度边界课程负荷；周末无 recurring course"
        courses = [
            _course("P04_CORE1", "核心课一", [1, 3], "08:00", "09:40", known_at),
            _course("P04_CORE2", "核心课二", [1, 3], "10:00", "11:40", known_at),
            _course("P04_LAB", "专业实验", [2, 4], "14:00", "17:40", known_at),
            _course("P04_SEMINAR", "晚间研讨", [5], "18:30", "20:10", known_at),
        ]
    return {
        "pack_id": pack_id,
        "participant_id": participant_id,
        "participant_context": {
            "text": f"两周 Context Pack（2026-09-07 至 2026-09-20）；{load}。Week 2 recurring course schedule 与 Week 1 相同。",
            "known_at": _iso(known_at),
            "source_ref": f"profile:{participant_id}",
        },
        "courses": courses,
    }


def _evidence(ref: str, text: str, at: datetime, participant_id: str, speaker: str = "PARTICIPANT") -> dict[str, Any]:
    return {
        "evidence_ref": ref,
        "speaker": speaker,
        "text": text,
        "known_at": _iso(at),
        "source_ref": f"conversation:{participant_id}",
    }


def _event(ref: str, title: str, description: str, at: datetime, participant_id: str, **facts: Any) -> dict[str, Any]:
    value = {
        "event_ref": ref,
        "title": title,
        "description": description,
        "known_at": _iso(at),
        "source_ref": f"scenario:{participant_id}",
    }
    value.update({key: (_iso(item) if isinstance(item, datetime) else item) for key, item in facts.items()})
    return value


def _bot(ref: str, text: str, sent_at: datetime, participant_id: str, **facts: Any) -> dict[str, Any]:
    value = {
        "response_unit_ref": ref,
        "text": text,
        "sent_at": _iso(sent_at),
        "known_at": _iso(sent_at),
        "source_ref": f"bot_log:{participant_id}",
    }
    value.update({key: (_iso(item) if isinstance(item, datetime) else item) for key, item in facts.items()})
    return value


def _scenario(
    number: int,
    pack_number: int,
    mode: str,
    modules: list[str],
    title: str,
    narrative: str,
    tags: list[str],
    *,
    focal_events: list[dict[str, Any]],
    evidence: list[dict[str, Any]] | None = None,
    tasks: list[dict[str, Any]] | None = None,
    recent: list[dict[str, Any]] | None = None,
    bot_units: list[dict[str, Any]] | None = None,
    cutoff: datetime | None = None,
    anchor: tuple[str, str, Any, str] | None = None,
) -> ScenarioArtifact:
    pack = _pack(pack_number)
    scenario_id = f"CAL_{number:03d}"
    cutoff = cutoff or (CALIBRATION_START + timedelta(days=number % 12, hours=12))
    visible = {
        "scenario_id": scenario_id,
        "scenario_version": "0.1",
        "pack_id": pack["pack_id"],
        "participant_id": pack["participant_id"],
        "presentation_mode": mode,
        "annotation_modules": modules,
        "annotation_time": _iso(cutoff),
        "known_at_cutoff": _iso(cutoff),
        "participant_context": pack["participant_context"],
        "recurring_course_context": pack["courses"],
        "recent_context": recent or [],
        "current_tasks": tasks or [],
        "focal_window": {
            "start": _iso(cutoff - timedelta(hours=2)),
            "end": _iso(cutoff),
            "narrative": narrative,
        },
        "focal_events": focal_events,
        "observed_conversation_evidence": evidence or [],
        "bot_response_units": bot_units or [],
        "source_refs": sorted(
            {
                pack["participant_context"]["source_ref"],
                *(course["source_ref"] for course in pack["courses"]),
                *(item["source_ref"] for item in (recent or [])),
                *(item["source_ref"] for item in (tasks or [])),
                *(item["source_ref"] for item in focal_events),
                *(item["source_ref"] for item in (evidence or [])),
                *(item["source_ref"] for item in (bot_units or [])),
            }
        ),
    }
    coverage = {"scenario_id": scenario_id, "coverage_tags": tags}
    anchor_value = None
    if anchor:
        target_ref, variable, intended_label, rationale = anchor
        anchor_value = {
            "anchor_id": f"ANCHOR_{scenario_id}_{variable}",
            "scenario_id": scenario_id,
            "target_ref": target_ref,
            "variable": variable,
            "intended_label": intended_label,
            "rationale": rationale,
            "reference_type": "DESIGN_ANCHOR",
            "is_gold": False,
        }
    return ScenarioArtifact(visible, coverage, anchor_value)


def build_calibration() -> tuple[list[ScenarioArtifact], list[dict[str, Any]]]:
    artifacts: list[ScenarioArtifact] = []

    def at(day: int, hour: int, minute: int = 0) -> datetime:
        return CALIBRATION_START + timedelta(days=day, hours=hour - 8, minutes=minute)

    # 1–2: same scheduled-only course in natural/structured presentation.
    for number, mode, narrative in (
        (1, "NATURAL", "课表显示上午 10:00–11:40 有高等数学。到中午为止，资料中没有签到、对话或其他到课记录。"),
        (2, "STRUCTURED", "高等数学课表时间为 10:00–11:40。截至 12:00，可见资料中没有出席记录。"),
    ):
        cutoff = at(1, 12)
        event = _event("E_COURSE_SCHEDULED", "高等数学", "课表中的常规课程。", at(0, 8), "P01", scheduled_start=at(1, 10), scheduled_end=at(1, 11, 40))
        artifacts.append(_scenario(number, 1, mode, ["A"], "Scheduled vs Realized", narrative, ["COURSE", "LIFECYCLE", "SCHEDULED_VS_REALIZED", "BOUNDARY_UNKNOWN"], focal_events=[event], cutoff=cutoff))

    # 3–4: precise partial interval, dual presentation.
    for number, mode, narrative in (
        (3, "NATURAL", "课程原定 10:00–11:40，签到与离场记录显示只参加到 10:25。"),
        (4, "STRUCTURED", "统计学课表时间为 10:00–11:40；签到和离场记录分别为 10:00 与 10:25。资料中没有参与比例记录。"),
    ):
        cutoff = at(2, 12)
        event = _event("E_COURSE_PARTIAL_INTERVAL", "统计学", "有精确实际参与区间。", at(2, 10, 25), "P02", scheduled_start=at(2, 10), scheduled_end=at(2, 11, 40), actual_start=at(2, 10), actual_end=at(2, 10, 25))
        artifacts.append(_scenario(number, 2, mode, ["A"], "Course PARTIAL / actual interval", narrative, ["COURSE", "PARTIAL", "ACTUAL_INTERVAL", "SINGLE_EXPOSURE"], focal_events=[event], cutoff=cutoff, anchor=("E_COURSE_PARTIAL_INTERVAL", "PARTIAL_ENCODING_BASIS", "ACTUAL_INTERVAL", "精确实际区间是唯一主要 partial 编码路径。")))

    # 5–6: fraction-only partial, dual presentation.
    for number, mode, narrative in (
        (5, "NATURAL", "参与者只记得实验课大约上了一半，无法回忆到离场时间。"),
        (6, "STRUCTURED", "参与者报告自己参加了大约一半的实验课，但没有提供到场和离场的具体时间。"),
    ):
        cutoff = at(3, 18)
        event = _event("E_COURSE_PARTIAL_FRACTION", "计算机实验", "参与者只提供了大致参与比例。", cutoff - timedelta(minutes=10), "P02", scheduled_start=at(3, 14), scheduled_end=at(3, 15, 40), exposure_fraction=0.5)
        artifacts.append(_scenario(number, 2, mode, ["A"], "Course PARTIAL / fraction only", narrative, ["COURSE", "PARTIAL", "FRACTION_ONLY", "SINGLE_EXPOSURE"], focal_events=[event], cutoff=cutoff, anchor=("E_COURSE_PARTIAL_FRACTION", "PARTIAL_ENCODING_BASIS", "FRACTION_ONLY", "没有 actual interval，比例是唯一明确编码。")))

    # 7–8: missed course without a catch-up obligation, dual presentation.
    for number, mode, narrative in (
        (7, "NATURAL", "参与者明确说今天高数没去；没有提补课、补录像或任何未来行动。"),
        (8, "STRUCTURED", "参与者明确表示没有参加当天的高等数学；截至当前，没有提到补课、补看录像或其他后续安排。"),
    ):
        cutoff = at(4, 12)
        event = _event("E_COURSE_SKIPPED", "高等数学", "课表记录与参与者陈述见当前可见资料。", cutoff - timedelta(minutes=15), "P01", scheduled_start=at(4, 10), scheduled_end=at(4, 11, 40))
        evidence = [_evidence("MSG_SKIPPED", "今天高数我明确没去。", cutoff - timedelta(minutes=15), "P01")]
        artifacts.append(_scenario(number, 1, mode, ["A"], "Skipped course without obligation", narrative, ["COURSE", "SKIPPED", "MISSED_NOT_OBLIGATION", "ANCHOR"], focal_events=[event], evidence=evidence, cutoff=cutoff, anchor=("E_COURSE_SKIPPED", "LIFECYCLE", "SKIPPED", "参与者明确报告未出席。")))

    cutoff = at(5, 13)
    catchup = _event("E_COURSE_CATCHUP", "补看高数录像", "参与者安排在今晚补看课程录像。", cutoff - timedelta(minutes=5), "P01", deadline=at(5, 21), progress=0, estimated_total_effort=2, remaining_effort=2, parent_ref="E_COURSE_MISSED_PARENT")
    evidence = [_evidence("MSG_009", "上午没去高数，我承诺今晚九点前把两小时录像补完。", cutoff - timedelta(minutes=5), "P01")]
    artifacts.append(_scenario(9, 1, "NATURAL", ["A"], "Skipped course with catch-up obligation", "参与者上午没有参加高等数学，并表示会在当晚九点前补看两小时课程录像；截至当前尚未开始。", ["COURSE", "TASK", "OBLIGATION", "MISSED_WITH_CATCHUP"], focal_events=[catchup], tasks=[catchup], evidence=evidence, cutoff=cutoff))

    cutoff = at(6, 11)
    open_task = _event("E_TASK_OPEN", "研究方法文献摘要", "任务已发布但未开始。", at(6, 9), "P03", deadline=at(9, 20), progress=0, estimated_total_effort=4, remaining_effort=4)
    artifacts.append(_scenario(10, 3, "STRUCTURED", ["A"], "Task OPEN", "文献摘要任务已发布，截止时间为本周三 20:00。任务记录显示完成比例为 0，资料中没有开始处理的记录。", ["TASK", "OPEN", "OBLIGATION"], focal_events=[open_task], tasks=[open_task], cutoff=cutoff))

    cutoff = at(6, 16)
    doing_task = _event("E_TASK_PROGRESS", "研究方法文献摘要", "已经开始并完成约一半。", cutoff - timedelta(minutes=10), "P03", deadline=at(9, 20), progress=0.5, estimated_total_effort=4, remaining_effort=2)
    artifacts.append(_scenario(11, 3, "NATURAL", ["A"], "Task IN_PROGRESS", "参与者已完成两篇中的一篇，仍需约两小时。", ["TASK", "IN_PROGRESS", "OBLIGATION"], focal_events=[doing_task], tasks=[doing_task], cutoff=cutoff))

    cutoff = at(7, 10)
    blocked = _event("E_TASK_BLOCKED", "研究数据清洗", "必须等待导师提供解密密钥才能继续。", cutoff - timedelta(minutes=10), "P03", deadline=at(10, 20), progress=0.25, estimated_total_effort=8, remaining_effort=6)
    evidence = [_evidence("MSG_012", "不是我不想做，没有导师的解密密钥我现在一步也推进不了。", cutoff - timedelta(minutes=10), "P03")]
    artifacts.append(_scenario(12, 3, "NATURAL", ["A", "B"], "Blocked task", "研究数据文件需要导师提供解密密钥。参与者目前尚未收到密钥，并表示在收到前无法继续处理文件。", ["TASK", "BLOCKED", "DEPENDENCY", "CROSS_LAYER_TRAP"], focal_events=[blocked], tasks=[blocked], evidence=evidence, cutoff=cutoff))

    cutoff = at(7, 22)
    overdue = _event("E_TASK_OVERDUE", "统计作业", "deadline 已过但 obligation 继续存在。", cutoff - timedelta(minutes=5), "P02", deadline=at(7, 20), progress=0.75, estimated_total_effort=4, remaining_effort=1)
    evidence = [_evidence("MSG_013", "已经过截止时间了，但老师允许迟交，我今晚继续做完。", cutoff - timedelta(minutes=5), "P02")]
    artifacts.append(_scenario(13, 2, "NATURAL", ["A"], "Overdue continuing obligation", "统计作业原定 20:00 截止。当前时间为 22:00；参与者说老师允许迟交，自己会在今晚继续完成剩余部分。", ["TASK", "OVERDUE", "OBLIGATION_CONTINUES"], focal_events=[overdue], tasks=[overdue], evidence=evidence, cutoff=cutoff))

    cutoff = at(8, 12)
    parent = _event("E_PROJECT_PARENT", "课程项目", "概括性 parent obligation。", cutoff - timedelta(hours=2), "P02", deadline=at(12, 20), progress=0.25, remaining_effort="UNKNOWN")
    child = _event("E_PROJECT_CHILD", "完成项目数据图", "当前唯一 active child。", cutoff - timedelta(minutes=10), "P02", deadline=at(9, 20), progress=0.5, remaining_effort=2, parent_ref="E_PROJECT_PARENT")
    artifacts.append(_scenario(14, 2, "STRUCTURED", ["A"], "Parent-child active leaf", "课程项目尚未完成。任务记录显示“完成项目数据图”属于该课程项目，目前已完成一半，仍需约两小时。", ["TASK", "PARENT_CHILD", "ACTIVE_LEAF", "DOUBLE_COUNT_TRAP"], focal_events=[parent, child], tasks=[parent, child], cutoff=cutoff))

    cutoff = at(8, 18)
    empty = _event("E_EMPTY_WINDOW", "周六下午空档", "日历没有安排；没有活动发生证据。", cutoff - timedelta(hours=1), "P03", scheduled_start=at(8, 14), scheduled_end=at(8, 18))
    artifacts.append(_scenario(15, 3, "NATURAL", ["A"], "Free time is not recovery", "周六下午 14:00–18:00 的日历没有安排。截至 18:00，参与者没有报告这段时间做了什么。", ["WEEKEND", "FREE_TIME", "RECOVERY_TRAP"], focal_events=[empty], cutoff=cutoff, anchor=("E_EMPTY_WINDOW", "RECOVERY_OCCURRENCE", "INACTIVE", "空闲本身不是 recovery occurrence。")))

    cutoff = at(9, 18)
    walk = _event("E_WALK", "湖边散步", "自主、无任务内容的 45 分钟散步。", cutoff - timedelta(minutes=5), "P03", actual_start=at(9, 16), actual_end=at(9, 16, 45))
    evidence = [_evidence("MSG_016", "下午我沿湖走了四十五分钟，手机也放在包里。", cutoff - timedelta(minutes=5), "P03")]
    artifacts.append(_scenario(16, 3, "NATURAL", ["A", "B"], "Recovery occurrence without fit evidence", "参与者报告下午 16:00–16:45 沿湖散步，并把手机放在包里。资料中没有其他关于散步后体验的陈述。", ["RECOVERY", "OCCURRENCE", "F_REC_NO_EVIDENCE"], focal_events=[walk], evidence=evidence, cutoff=cutoff))

    cutoff = at(9, 20)
    walk_fit = _event("E_WALK_FIT", "湖边散步", "同类散步活动。", cutoff - timedelta(minutes=5), "P03", actual_start=at(9, 18), actual_end=at(9, 18, 45))
    evidence = [_evidence("MSG_017", "这种不看手机的散步通常能让我真正缓过来。", cutoff - timedelta(minutes=5), "P03")]
    artifacts.append(_scenario(17, 3, "NATURAL", ["A", "B"], "Recovery fit evidence", "参与者报告下午 18:00–18:45 沿湖散步，并说这种不看手机的散步通常能让自己真正缓过来。", ["RECOVERY", "F_REC", "EVENT_CLASS_EVIDENCE"], focal_events=[walk_fit], evidence=evidence, cutoff=cutoff))

    cutoff = at(10, 12)
    hard_task = _event("E_HARD_TASK", "算法形式证明", "任务要求独立完成包含五个推导步骤的形式证明。", cutoff - timedelta(minutes=10), "P04", deadline=at(11, 20), progress=0, remaining_effort=6)
    artifacts.append(_scenario(18, 4, "STRUCTURED", ["A", "B"], "Difficulty is not C_exec", "算法作业要求独立完成包含五个推导步骤的形式证明，预计仍需六小时。资料中没有参与者对自己能否完成的陈述。", ["D_POT", "C_EXEC", "FORBIDDEN_INFERENCE"], focal_events=[hard_task], tasks=[hard_task], cutoff=cutoff))

    cutoff = at(10, 13)
    hard_task_2 = deepcopy(hard_task)
    hard_task_2["event_ref"] = "E_HARD_TASK_LOW_CEXEC"
    hard_task_2["known_at"] = _iso(cutoff - timedelta(minutes=5))
    evidence = [_evidence("MSG_019", "证明要求我看懂了，但我完全不知道怎么下手，今天肯定做不出来。", cutoff - timedelta(minutes=5), "P04")]
    artifacts.append(_scenario(19, 4, "NATURAL", ["A", "B"], "Same difficulty, explicit C_exec", "算法作业要求独立完成包含五个推导步骤的形式证明，预计仍需六小时。参与者说自己看懂了要求，但完全不知道如何下手，认为今天做不出来。", ["D_POT", "C_EXEC", "MINIMAL_CONTRAST"], focal_events=[hard_task_2], tasks=[hard_task_2], evidence=evidence, cutoff=cutoff))

    cutoff = at(10, 16)
    required = _event("E_REQUIRED_COURSE", "专业必修期中考试", "4 学分必修课考试。", cutoff - timedelta(minutes=5), "P04", scheduled_start=at(11, 14), scheduled_end=at(11, 15, 40))
    artifacts.append(_scenario(20, 4, "STRUCTURED", ["A", "B"], "Objective stakes are not importance", "明天下午有一场专业必修课期中考试，该课程为 4 学分。资料中没有参与者谈及这场考试对自己的意义或优先级。", ["IMPORTANCE", "OBJECTIVE_STAKES", "FORBIDDEN_INFERENCE"], focal_events=[required], cutoff=cutoff))

    cutoff = at(10, 18)
    controllability = _event("E_EXEC_OUTCOME", "课程报告修订", "课程报告仍可修改；系统显示最终成绩已经锁定。", cutoff - timedelta(minutes=5), "P04", deadline=at(11, 20), progress=0.5, remaining_effort=2)
    evidence = [_evidence("MSG_021", "我知道怎么改，也肯定能改完；但成绩已经锁定，再改也影响不了结果。", cutoff - timedelta(minutes=5), "P04")]
    artifacts.append(_scenario(21, 4, "NATURAL", ["B"], "C_exec differs from C_out", "课程报告还可以继续修改，但系统显示最终成绩已经锁定。参与者说自己知道怎么改、肯定能改完，同时认为修改已经影响不了成绩。", ["C_EXEC", "C_OUT", "APPRAISAL_BOUNDARY"], focal_events=[controllability], tasks=[controllability], evidence=evidence, cutoff=cutoff))

    cutoff = at(11, 14)
    uncertain_structure = _event("E_RULES_UNKNOWN", "临时课程展示", "题目和评分规则尚未公布。", cutoff - timedelta(minutes=10), "P04", scheduled_start=at(11, 16), scheduled_end=at(11, 16, 45))
    evidence = [_evidence("MSG_022", "规则还没发，不过类似展示我做过很多，我很确定能应付。", cutoff - timedelta(minutes=10), "P04")]
    bot = [_bot("BOT_022", "先确认已知要求，再列两个你能控制的准备动作。", cutoff - timedelta(minutes=5), "P04", interaction_context="用户主动询问准备方法")]
    artifacts.append(_scenario(22, 4, "NATURAL", ["A", "B", "C"], "High U_context / low U_perc", "课程展示将在当天下午进行。截至当前，题目和评分规则尚未公布。参与者表示自己以前做过多次类似展示，并认为自己能够应付。随后机器人建议先确认已知要求，再列出两个可控制的准备动作。", ["U_CONTEXT", "U_PERC", "TASK_HELP", "BOT"], focal_events=[uncertain_structure], evidence=evidence, bot_units=bot, cutoff=cutoff))

    cutoff = at(11, 15)
    clear_structure = _event("E_RULES_CLEAR", "实验报告", "模板、评分规则和依赖均已公布。", cutoff - timedelta(minutes=10), "P04", deadline=at(12, 20), progress=0.25, remaining_effort=4)
    evidence = [_evidence("MSG_023", "要求都很清楚，但我还是完全没底，也一直绷着。", cutoff - timedelta(minutes=10), "P04")]
    bot = [_bot("BOT_023", "要求都清楚但你还是没底，这种绷着的感觉很难受。先慢慢呼吸三次，然后只选一个最小步骤；我可以陪你一起定。", cutoff - timedelta(minutes=5), "P04", interaction_context="用户表达主观不确定和紧张", read_at=cutoff - timedelta(minutes=1))]
    artifacts.append(_scenario(23, 4, "NATURAL", ["A", "B", "C"], "Low U_context / high U_perc", "实验报告的模板、评分规则和依赖均已公布。参与者说要求都很清楚，但自己仍然完全没底、一直绷着。机器人回应了这种感受，并建议先做三次慢呼吸，再选择一个最小步骤。", ["U_CONTEXT", "U_PERC", "EMOTIONAL_SUPPORT", "COPING_SUPPORT", "SEEN"], focal_events=[clear_structure], tasks=[clear_structure], evidence=evidence, bot_units=bot, cutoff=cutoff))

    cutoff = at(11, 15)
    appointment = _event("E_APPOINTMENT_PENDING", "导师会面", "截至 15:00 日历仍显示 16:00 会面；系统状态为 pending。", cutoff - timedelta(minutes=15), "P01", scheduled_start=at(11, 16), scheduled_end=at(11, 16, 45))
    recent = [_evidence("SYS_024", "15:00 查询结果：会面仍为 pending，未收到取消。", cutoff, "P01", speaker="SYSTEM")]
    bot = [_bot("BOT_024", "离会面还有一小时，如果你愿意，我们先用两分钟整理最想问的一个问题。", cutoff - timedelta(minutes=10), "P01", interaction_context="发送时会面仍显示 pending")]
    artifacts.append(_scenario(24, 1, "STRUCTURED", ["A", "C"], "Known now vs known later", "当前时间为 15:00。日历仍显示 16:00 与导师会面，系统查询结果为 pending，尚未收到取消通知。机器人在 14:50 建议先用两分钟整理最想问的一个问题。", ["KNOWN_AT", "FUTURE_LEAKAGE", "BOT_RELEVANCE", "REVISION_TIMING", "RECOVERY_SUGGESTION"], focal_events=[appointment], recent=recent, bot_units=bot, cutoff=cutoff))

    pairs = [
        _pair("PRES_SCHEDULED", ["CAL_001", "CAL_002"], "presentation_mode", [], ["EVENT_FAMILY", "LIFECYCLE", "EXECUTION_EXPOSURE"], ["NATURAL_STRUCTURED", "COURSE"], "同一 scheduled-only 事实的 natural/structured 双版本。", "PRESENTATION_EQUIVALENCE", "BETWEEN_GROUPS", {"LIFECYCLE": {"left_target_ref": "E_COURSE_SCHEDULED", "right_target_ref": "E_COURSE_SCHEDULED"}, "EXECUTION_EXPOSURE": {"left_target_ref": "E_COURSE_SCHEDULED", "right_target_ref": "E_COURSE_SCHEDULED"}}),
        _pair("PRES_PARTIAL_INTERVAL", ["CAL_003", "CAL_004"], "presentation_mode", [], ["EVENT_FAMILY", "LIFECYCLE", "EXECUTION_EXPOSURE", "PARTIAL_ENCODING_BASIS"], ["NATURAL_STRUCTURED", "PARTIAL"], "同一 actual interval partial 的 natural/structured 双版本。", "PRESENTATION_EQUIVALENCE", "BETWEEN_GROUPS", {"LIFECYCLE": {"left_target_ref": "E_COURSE_PARTIAL_INTERVAL", "right_target_ref": "E_COURSE_PARTIAL_INTERVAL"}, "EXECUTION_EXPOSURE": {"left_target_ref": "E_COURSE_PARTIAL_INTERVAL", "right_target_ref": "E_COURSE_PARTIAL_INTERVAL"}, "PARTIAL_ENCODING_BASIS": {"target_variable": "EXECUTION_EXPOSURE", "record_attribute": "partial_encoding_basis", "left_target_ref": "E_COURSE_PARTIAL_INTERVAL", "right_target_ref": "E_COURSE_PARTIAL_INTERVAL"}}),
        _pair("PRES_PARTIAL_FRACTION", ["CAL_005", "CAL_006"], "presentation_mode", [], ["EVENT_FAMILY", "LIFECYCLE", "EXECUTION_EXPOSURE", "PARTIAL_ENCODING_BASIS"], ["NATURAL_STRUCTURED", "PARTIAL"], "同一 fraction-only partial 的 natural/structured 双版本。", "PRESENTATION_EQUIVALENCE", "BETWEEN_GROUPS", {"LIFECYCLE": {"left_target_ref": "E_COURSE_PARTIAL_FRACTION", "right_target_ref": "E_COURSE_PARTIAL_FRACTION"}, "EXECUTION_EXPOSURE": {"left_target_ref": "E_COURSE_PARTIAL_FRACTION", "right_target_ref": "E_COURSE_PARTIAL_FRACTION"}, "PARTIAL_ENCODING_BASIS": {"target_variable": "EXECUTION_EXPOSURE", "record_attribute": "partial_encoding_basis", "left_target_ref": "E_COURSE_PARTIAL_FRACTION", "right_target_ref": "E_COURSE_PARTIAL_FRACTION"}}),
        _pair("PRES_SKIPPED", ["CAL_007", "CAL_008"], "presentation_mode", [], ["EVENT_FAMILY", "LIFECYCLE", "OBLIGATION_EXISTS"], ["NATURAL_STRUCTURED", "MISSED_NOT_OBLIGATION"], "同一明确缺席且无补做 commitment 的 natural/structured 双版本。", "PRESENTATION_EQUIVALENCE", "BETWEEN_GROUPS", {"LIFECYCLE": {"left_target_ref": "E_COURSE_SKIPPED", "right_target_ref": "E_COURSE_SKIPPED"}, "OBLIGATION_EXISTS": {"left_target_ref": "E_COURSE_SKIPPED", "right_target_ref": "E_COURSE_SKIPPED"}}),
        _pair("CONTRAST_MISSED_OBLIGATION", ["CAL_008", "CAL_009"], "catch_up_commitment", ["OBLIGATION_EXISTS"], [], ["OBLIGATION", "MINIMAL_CONTRAST"], "只在第二个场景加入实际未来行动承诺；比较明确标注的 missed course 与 catch-up obligation。", "MINIMAL_CONTRAST", "WITHIN_ANNOTATOR", {"OBLIGATION_EXISTS": {"left_target_ref": "E_COURSE_SKIPPED", "right_target_ref": "E_COURSE_CATCHUP"}}),
        _pair("CONTRAST_RECOVERY_FIT", ["CAL_016", "CAL_017"], "explicit_recovery_fit_evidence", ["F_REC"], ["RECOVERY_OCCURRENCE", "R_POT"], ["RECOVERY", "APPRAISAL"], "活动均发生，只有第二个有 participant-specific fit evidence。", "MINIMAL_CONTRAST", "WITHIN_ANNOTATOR", {"F_REC": {"left_target_ref": "E_WALK", "right_target_ref": "E_WALK_FIT"}, "RECOVERY_OCCURRENCE": {"left_target_ref": "E_WALK", "right_target_ref": "E_WALK_FIT"}, "R_POT": {"left_target_ref": "E_WALK", "right_target_ref": "E_WALK_FIT"}}),
        _pair("CONTRAST_DIFFICULTY_CEXEC", ["CAL_018", "CAL_019"], "explicit_c_exec_evidence", ["C_EXEC"], ["D_POT"], ["APPRAISAL", "CROSS_LAYER_TRAP"], "objective task requirements 保持不变。", "MINIMAL_CONTRAST", "WITHIN_ANNOTATOR", {"C_EXEC": {"left_target_ref": "E_HARD_TASK", "right_target_ref": "E_HARD_TASK_LOW_CEXEC"}, "D_POT": {"left_target_ref": "E_HARD_TASK", "right_target_ref": "E_HARD_TASK_LOW_CEXEC"}}),
        _pair("CONTRAST_UNCERTAINTY_2X2", ["CAL_022", "CAL_023"], "structural_vs_perceived_uncertainty", ["U_CONTEXT", "U_PERC"], [], ["U_CONTEXT", "U_PERC", "ORTHOGONAL"], "两个因素形成交叉边界：结构信息清楚程度与个体主观不确定性分别变化。", "ORTHOGONAL_CROSS", "WITHIN_ANNOTATOR", {"U_CONTEXT": {"left_target_ref": "E_RULES_UNKNOWN", "right_target_ref": "E_RULES_CLEAR"}, "U_PERC": {"left_target_ref": "E_RULES_UNKNOWN", "right_target_ref": "E_RULES_CLEAR"}}),
    ]
    return artifacts, pairs


def _pair(
    pair_id: str,
    scenario_ids: list[str],
    factor: str,
    sensitive: list[str],
    invariant: list[str],
    tags: list[str],
    notes: str,
    pair_kind: str,
    comparison_mode: str,
    target_mapping: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    pair = {
        "pair_id": pair_id,
        "scenario_ids": scenario_ids,
        "manipulated_factor": factor,
        "comparison_mode": comparison_mode,
        "pair_kind": pair_kind,
        "expected_sensitive_constructs": sensitive,
        "expected_invariant_constructs": invariant,
        "coverage_tags": tags,
        "design_notes": notes,
    }
    if target_mapping:
        pair["target_mapping"] = target_mapping
    return pair


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def write_calibration(output_root: str | Path) -> dict[str, int]:
    root = Path(output_root)
    artifacts, pairs = build_calibration()
    _write_jsonl(root / "scenarios" / "calibration.jsonl", (item.visible for item in artifacts))
    _write_jsonl(root / "hidden" / "coverage_tags.jsonl", (item.coverage for item in artifacts))
    _write_jsonl(root / "hidden" / "pair_design.jsonl", pairs)
    _write_jsonl(root / "hidden" / "anchor_reference.jsonl", (item.anchor for item in artifacts if item.anchor))
    return {
        "scenarios": len(artifacts),
        "pairs": len(pairs),
        "anchors": sum(item.anchor is not None for item in artifacts),
    }
