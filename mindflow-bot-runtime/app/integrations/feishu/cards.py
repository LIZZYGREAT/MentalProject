"""Reviewed Feishu card builders for participant-visible rich replies."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.domain.course_schedule_recurrence import (
    EXPAND_ALL_OCCURRENCES,
    PRESERVE_SCHEDULE_PATTERN,
    describe_course_write_plan,
    plan_course_writes,
)


_UNCERTAIN_FIELD_LABELS = {
    "weekday": "星期",
    "period_start": "节次",
    "period_end": "节次",
    "start_time": "上课时间",
    "end_time": "上课时间",
    "actual_time": "上课时间",
    "week_rule": "周次",
    "location": "地点",
    "teacher": "教师",
}


def rich_answer_card(markdown_text: str) -> dict[str, Any]:
    """Non-streaming Card 2.0 fallback for a validated Markdown answer."""

    content = str(markdown_text or "").strip()
    if not content:
        raise ValueError("rich answer markdown is required")
    return {
        "schema": "2.0",
        "config": {"summary": {"content": "MindFlow"}},
        "header": {
            "title": {"tag": "plain_text", "content": "MindFlow"},
        },
        "body": {
            "elements": [{"tag": "markdown", "content": content}],
        },
    }


def _calendar_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value or "").strip()
        if not raw:
            raise ValueError("calendar datetime is required")
        if raw.endswith(("Z", "z")):
            raw = f"{raw[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise ValueError("calendar datetime is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("calendar datetime must include a timezone")
    return parsed


def _format_calendar_datetime_range(
    start: object,
    end: object,
    *,
    timezone_name: str = "Asia/Shanghai",
) -> str:
    """Format an aware Calendar range without exposing transport syntax."""

    display_timezone = ZoneInfo(timezone_name)
    local_start = _calendar_datetime(start).astimezone(display_timezone)
    local_end = _calendar_datetime(end).astimezone(display_timezone)
    if local_end <= local_start:
        raise ValueError("calendar end time must be after start time")
    if local_start.date() == local_end.date():
        return (
            f"{local_start:%Y-%m-%d %H:%M}"
            f"–{local_end:%H:%M}"
        )
    return f"{local_start:%Y-%m-%d %H:%M} – {local_end:%Y-%m-%d %H:%M}"


def _natural_uncertain_fields(fields: list[Any]) -> list[str]:
    labels: list[str] = []
    for field in fields:
        label = _UNCERTAIN_FIELD_LABELS.get(str(field), "其他信息")
        if label not in labels:
            labels.append(label)
    return labels


def course_schedule_preview_card(draft: dict[str, Any]) -> dict[str, Any]:
    """Fixed schedule preview; callback carries only the opaque draft id."""

    structured = dict(draft.get("structured_result") or {})
    courses = list(structured.get("courses") or [])
    items = list(draft.get("items") or [])
    missing = set(structured.get("missing_context") or [])
    metadata = dict(structured.get("_metadata") or {})
    time_sources = list(metadata.get("course_time_sources") or [])
    status = str(draft.get("status") or "")
    edit_enabled = (
        status in {"pending_context", "pending_confirmation"}
        and not draft.get("recurrence_strategy")
    )
    elements: list[dict[str, Any]] = [{
        "tag": "markdown",
        "content": f"识别到 **{len(courses)}** 门课",
    }]
    uncertain_count = 0
    weekday_names = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
    for index, course in enumerate(courses[:20]):
        name = _safe_schedule_text(course.get("course_name") or "未命名课程")
        weekday = course.get("weekday")
        day = weekday_names[int(weekday) - 1] if isinstance(weekday, int) and 1 <= weekday <= 7 else "星期待确认"
        start = course.get("start_time")
        end = course.get("end_time")
        period = (
            f"第{course.get('period_start')}–{course.get('period_end')}节"
            if course.get("period_start") and course.get("period_end")
            else "节次待确认"
        )
        actual_time = f"{start}–{end}" if start and end else "时间待确认"
        rule = dict(course.get("week_rule") or {})
        if rule.get("explicit_weeks"):
            week_text = ",".join(str(value) for value in rule["explicit_weeks"]) + "周"
        elif rule.get("start_week") and rule.get("end_week"):
            week_text = f"{rule.get('start_week')}–{rule.get('end_week')}周"
            if rule.get("odd_even") == "odd":
                week_text += "单周"
            elif rule.get("odd_even") == "even":
                week_text += "双周"
        else:
            week_text = "周次待确认"
        location = _safe_schedule_text(course.get("location") or "地点待确认")
        course_lines = [
            f"{index + 1}. **{name}**",
            f"{day} · {period} · {actual_time} · {week_text} · {location}",
        ]
        source = time_sources[index] if index < len(time_sources) else None
        source_text = {
            "image": "课表图片中的实际时间",
            "user": "你刚刚提供的作息",
            "user_actual": "你刚刚提供的实际时间",
            "default": "学校默认作息",
        }.get(source)
        if source_text:
            course_lines.append(f"时间来源：{source_text}")
        if not missing and index < len(items):
            writes = plan_course_writes(
                draft,
                items[index],
                strategy=PRESERVE_SCHEDULE_PATTERN,
                timezone=ZoneInfo(str(draft.get("timezone") or "Asia/Shanghai")),
            )
            course_lines.append(f"重复方式：{describe_course_write_plan(writes)}")
        fields = list(course.get("uncertain_fields") or [])
        if fields:
            uncertain_count += 1
            course_lines.append(
                f"待确认：{', '.join(_natural_uncertain_fields(fields))}"
            )
        elements.append({"tag": "markdown", "content": "\n".join(course_lines)})
        if (
            edit_enabled
            and index < len(items)
            and str(items[index].get("id") or "").strip()
        ):
            elements.append(_schedule_item_edit_button(draft["id"], items[index]))

    global_sections: list[str] = []
    if len(courses) > 20:
        global_sections.append("课程数量超过 20 项，请拆分图片后重新导入。")
    warnings = [_safe_schedule_text(value) for value in structured.get("warnings") or []]
    parse_report = dict(metadata.get("parse_report") or {})
    quarantined = list(parse_report.get("quarantined") or [])
    over_limit = [
        item
        for item in quarantined
        if item.get("reason") == "course_item_limit_exceeded"
    ]
    other_quarantined = [item for item in quarantined if item not in over_limit]
    if over_limit:
        global_sections.append(
            f"另有 {len(over_limit)} 门课超出单次 20 门上限，未放入本次预览。\n"
            "请拆成两张图重新发送，例如先发周一到周三，再发周四到周日。"
        )
    if other_quarantined:
        quarantined_lines = [
            f"另有 {len(other_quarantined)} 门课需要补充后再并入："
        ]
        quarantined_lines.extend(
            f"- {_safe_schedule_text(item.get('course_name') or '未命名课程')}"
            "（识别格式待确认）"
            for item in other_quarantined[:10]
        )
        global_sections.append("\n".join(quarantined_lines))
    if uncertain_count or warnings:
        review_lines = [f"有 {uncertain_count + len(warnings)} 项需要你确认"]
        review_lines.extend(f"- {value}" for value in warnings)
        global_sections.append("\n".join(review_lines))
    duplicate_warning = dict(draft.get("duplicate_warning") or {})
    duplicate_names = [
        _safe_schedule_text(value)
        for value in duplicate_warning.get("course_names") or []
    ]
    if duplicate_names:
        duplicate_lines = [
            f"⚠️ 近期导入中有 {len(duplicate_names)} 门课可能重复：",
            *[f"- {name}" for name in duplicate_names[:10]],
            "你仍可继续确认；如日历中已存在，请先核对，避免重复添加。",
        ]
        global_sections.append("\n".join(duplicate_lines))
    if "semester_start_date" in missing:
        global_sections.append(
            "还差一个信息：这学期第一周周一是哪天？例如 2026-09-07。"
        )
    if "weekday" in missing:
        global_sections.append("有课程的星期还不确定，请告诉我对应课程是周几。")
    if "week_rule" in missing:
        global_sections.append(
            "有课程的周次还不确定，请告诉我起止周，或说明单周/双周。"
        )
    if missing & {"period_time_mapping", "actual_time"}:
        global_sections.append(
            "有课程的节次或实际时间还不确定，请告诉我是第几节到第几节，"
            "或直接提供起止时间。"
        )

    action_elements: list[dict[str, Any]] = []
    if missing and status == "pending_context":
        global_sections.append(
            "填写关键信息后，我会先给出完整预览；确认前不会添加到日历。"
        )
        action_elements.extend([
            _schedule_context_button(draft["id"]),
            _schedule_cancel_button(draft["id"]),
        ])
    elif not missing and len(courses) <= 20 and status == "pending_confirmation":
        global_sections.append(
            "按课程规律添加：例如“每周一第1-2节”，在日历里按周重复。\n"
            "每次单独添加：每一次上课都创建成独立日程。\n\n"
            "确认无误后，我再添加到日历。"
        )
        action_elements.extend([
            _schedule_action_button(
                draft["id"],
                "按课程规律添加（推荐）",
                strategy=PRESERVE_SCHEDULE_PATTERN,
                primary=True,
            ),
            _schedule_action_button(
                draft["id"],
                "每次上课都单独添加",
                strategy=EXPAND_ALL_OCCURRENCES,
            ),
            _schedule_cancel_button(draft["id"]),
        ])
    elif not missing and status == "partial_failed" and draft.get("recurrence_strategy"):
        action_elements.append(_schedule_action_button(
            draft["id"],
            "重试失败项",
            strategy=str(draft["recurrence_strategy"]),
            primary=True,
        ))
    elif status in {"queued", "running", "cancelling", "cleanup_failed"}:
        if status == "cancelling":
            global_sections.append("正在停止导入，并清理已经添加的课程…")
        elif status == "cleanup_failed":
            global_sections.append("导入已停止，但还有部分日程尚未清理完成。")
        else:
            global_sections.append("正在添加到日历，请稍候。")
        if status in {"queued", "running"}:
            action_elements.append(
                _schedule_cancel_button(draft["id"], "取消并撤销")
            )
    elif status == "succeeded":
        global_sections.append("这份课程表已经添加到日历。")
    elif status in {"cancelled", "expired"}:
        global_sections.append("这份课程表导入已取消或过期，请重新发送图片。")

    if global_sections:
        elements.append({
            "tag": "markdown",
            "content": "\n\n".join(global_sections),
        })
    elements.extend(action_elements)
    return {
        "schema": "2.0",
        "config": {"update_multi": True, "enable_forward": False},
        "header": {"template": "blue", "title": {"tag": "plain_text", "content": "课程表识别结果"}},
        "body": {"direction": "vertical", "elements": elements},
    }


def course_schedule_result_card(
    message: str,
    *,
    status: str | None = None,
    import_id: str | None = None,
    error: str | None = None,
    recurrence_strategy: str | None = None,
) -> dict[str, Any]:
    elements: list[dict[str, Any]] = [
        {"tag": "markdown", "content": _safe_schedule_text(message)}
    ]
    if (
        error == "calendar_not_connected"
        and import_id
        and recurrence_strategy
        and status != "cleanup_failed"
    ):
        elements.append(_schedule_action_button(
            import_id,
            "重试失败项" if status == "partial_failed" else "继续按已选择策略添加",
            strategy=recurrence_strategy,
            primary=True,
        ))
        if status != "partial_failed":
            elements.append(_schedule_cancel_button(import_id))
    elif (
        error != "provider_event_identity_conflict"
        and status == "partial_failed"
        and import_id
        and recurrence_strategy
    ):
        elements.append(_schedule_action_button(
            import_id,
            "重试失败项",
            strategy=recurrence_strategy,
            primary=True,
        ))
    elif error == "calendar_write_limit_exceeded" and import_id:
        if recurrence_strategy == EXPAND_ALL_OCCURRENCES:
            elements.append(_schedule_action_button(
                import_id,
                "改用按课表周期规则添加",
                strategy=PRESERVE_SCHEDULE_PATTERN,
                primary=True,
            ))
        elements.append(_schedule_cancel_button(import_id))
    template = "green" if status in {"succeeded", "cancelled"} else "blue"
    return {
        "schema": "2.0",
        "config": {"update_multi": True, "enable_forward": False},
        "header": {"template": template, "title": {"tag": "plain_text", "content": "课程表导入"}},
        "body": {"direction": "vertical", "elements": elements},
    }


def _schedule_action_button(
    import_id: str,
    text: str,
    *,
    strategy: str,
    primary: bool = False,
) -> dict[str, Any]:
    return {
        "tag": "button",
        "type": "primary" if primary else "default",
        "text": {"tag": "plain_text", "content": text},
        "behaviors": [{"type": "callback", "value": {
            "mindflow_action": "course_schedule_import_confirm",
            "version": "2",
            "import_id": str(import_id),
            "recurrence_strategy": strategy,
        }}],
    }


def _schedule_cancel_button(
    import_id: str, text: str = "暂不导入"
) -> dict[str, Any]:
    return {
        "tag": "button",
        "type": "default",
        "text": {"tag": "plain_text", "content": text},
        "behaviors": [{"type": "callback", "value": {
            "mindflow_action": "course_schedule_import_cancel",
            "version": "2",
            "import_id": str(import_id),
        }}],
    }


def _schedule_context_button(import_id: str) -> dict[str, Any]:
    """Open the fixed, non-LLM context form for an incomplete draft."""

    return {
        "tag": "button",
        "type": "primary",
        "text": {"tag": "plain_text", "content": "补充信息并生成预览"},
        "behaviors": [{"type": "callback", "value": {
            "mindflow_action": "course_schedule_import_context_open",
            "version": "3",
            "import_id": str(import_id),
        }}],
    }


def course_schedule_revert_confirmation_card(
    candidate: dict[str, Any],
) -> dict[str, Any]:
    """Fixed destructive review for one backend-resolved completed import."""

    import_id = str(candidate.get("id") or "").strip()
    if not import_id or len(import_id) > 64:
        raise ValueError("course schedule import id is invalid")
    names = [str(name)[:80] for name in list(candidate.get("course_names") or [])]
    course_count = int(candidate.get("course_count") or len(names))
    event_count = int(candidate.get("provider_effect_count") or 0)
    lines = [
        f"将撤销 **{course_count}** 门课程对应的 **{event_count}** 个日程。",
        *(f"- {name}" for name in names[:10]),
    ]
    if course_count > len(names[:10]):
        lines.append(f"- 以及其余 {course_count - len(names[:10])} 门课程")
    lines.append("确认后会启动可恢复的 Calendar 清理流程。")
    return {
        "schema": "2.0",
        "config": {
            "update_multi": True,
            "width_mode": "fill",
            "enable_forward": False,
            "summary": {"content": "确认撤销课程表导入"},
        },
        "header": {
            "template": "red",
            "title": {"tag": "plain_text", "content": "确认撤销课程表导入"},
        },
        "body": {
            "direction": "vertical",
            "elements": [
                {"tag": "markdown", "content": "\n".join(lines)},
                {
                    "tag": "button",
                    "type": "danger",
                    "text": {"tag": "plain_text", "content": "确认撤销"},
                    "behaviors": [{
                        "type": "callback",
                        "value": {
                            "mindflow_action": "course_schedule_revert_confirm",
                            "version": "1",
                            "import_id": import_id,
                        },
                    }],
                },
                {
                    "tag": "button",
                    "type": "default",
                    "text": {"tag": "plain_text", "content": "取消"},
                    "behaviors": [{
                        "type": "callback",
                        "value": {
                            "mindflow_action": "course_schedule_revert_cancel",
                            "version": "1",
                            "import_id": import_id,
                        },
                    }],
                },
            ],
        },
    }


def _clock_select(
    field: str,
    label: str,
    selected: str,
    values: list[str],
    suffix: str,
) -> dict[str, Any]:
    return {
        "tag": "select_static",
        "name": field,
        "required": True,
        "placeholder": {"tag": "plain_text", "content": label},
        "initial_option": selected,
        "options": [
            {
                "text": {
                    "tag": "plain_text",
                    "content": f"{value} {suffix}",
                },
                "value": value,
            }
            for value in values
        ],
    }


def _clock_form_fields(start: str, end: str) -> list[dict[str, Any]]:
    try:
        start_hour, start_minute = start.split(":", 1)
        end_hour, end_minute = end.split(":", 1)
    except ValueError as exc:
        raise ValueError("clock values must use HH:MM") from exc
    hours = [f"{value:02d}" for value in range(24)]
    start_minutes = sorted(
        {f"{value:02d}" for value in range(0, 60, 5)} | {start_minute}
    )
    end_minutes = sorted(
        {f"{value:02d}" for value in range(0, 60, 5)} | {end_minute}
    )
    return [
        {"tag": "markdown", "content": "**开始时间**"},
        _clock_select("start_hour", "选择小时", start_hour, hours, "时"),
        _clock_select(
            "start_minute", "选择分钟", start_minute, start_minutes, "分"
        ),
        {"tag": "markdown", "content": "**结束时间**"},
        _clock_select("end_hour", "选择小时", end_hour, hours, "时"),
        _clock_select("end_minute", "选择分钟", end_minute, end_minutes, "分"),
    ]


def _schedule_item_edit_button(
    import_id: str,
    item: dict[str, Any],
) -> dict[str, Any]:
    return {
        "tag": "button",
        "type": "default",
        "text": {
            "tag": "plain_text",
            "content": "修改时间",
        },
        "behaviors": [{"type": "callback", "value": {
            "mindflow_action": "course_schedule_item_time_open",
            "version": "1",
            "import_id": str(import_id),
            "item_id": str(item["id"]),
        }}],
    }


def course_schedule_item_time_card(
    draft: dict[str, Any], item_id: str
) -> dict[str, Any]:
    """Fixed form bound to one opaque course item identifier."""

    items = list(draft.get("items") or [])
    item = next(
        (value for value in items if str(value.get("id") or "") == str(item_id)),
        None,
    )
    if item is None:
        raise LookupError("course schedule item not found")
    name = _safe_schedule_text(item.get("course_name") or "未命名课程")
    start = str(item.get("start_time") or "08:00")
    end = str(item.get("end_time") or "09:00")
    form_elements = [
        {
            "tag": "markdown",
            "content": (
                f"**{name}**\n当前时间：{start}–{end}\n"
                "修改只会刷新课程表预览，不会写入日历。"
            ),
        },
        *_clock_form_fields(start, end),
        {
            "tag": "button",
            "name": "course_schedule_item_time_submit",
            "type": "primary",
            "text": {"tag": "plain_text", "content": "确认修改"},
            "form_action_type": "submit",
            "behaviors": [{"type": "callback", "value": {
                "mindflow_action": "course_schedule_item_time_submit",
                "version": "2",
                "import_id": str(draft["id"]),
                "item_id": str(item_id),
            }}],
        },
    ]
    return {
        "schema": "2.0",
        "config": {"update_multi": True, "enable_forward": False},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": "修改课程时间"},
        },
        "body": {
            "direction": "vertical",
            "elements": [{
                "tag": "form",
                "name": "mindflow_course_schedule_item_time",
                "elements": form_elements,
            }],
        },
    }


def course_schedule_context_card(draft: dict[str, Any]) -> dict[str, Any]:
    """Collect the two common import prerequisites without invoking an LLM.

    This card only updates the participant-owned draft.  It never queues or
    writes Calendar events; the existing preview card remains the sole place
    where the participant chooses a recurrence strategy and confirms import.
    """

    structured = dict(draft.get("structured_result") or {})
    missing = set(structured.get("missing_context") or [])
    elements: list[dict[str, Any]] = [
        {
            "tag": "markdown",
            "content": (
                "先补齐课表的关键信息。提交后会显示完整预览，"
                "由你选择重复方式；这一步不会添加日历。"
            ),
        }
    ]
    if "semester_start_date" in missing:
        elements.extend([
            {
                "tag": "markdown",
                "content": "**第一周周一**（必填）",
            },
            {
                "tag": "input",
                "name": "semester_start_date",
                "required": True,
                "max_length": 10,
                "placeholder": {
                    "tag": "plain_text",
                    "content": "例如 2026-09-07、2026/9/7 或 9月7日",
                },
                "label": {"tag": "plain_text", "content": "第一周周一"},
            },
        ])
    if missing & {"period_time_mapping", "actual_time"}:
        elements.extend([
            {
                "tag": "markdown",
                    "content": (
                        "**节次时间对照**（必填）\n"
                        "每行一条，例如：`1-2节：8:00-9:35`\n"
                        "可继续填写：`3-4=10:00-11:35`"
                ),
            },
            {
                "tag": "input",
                "name": "period_time_mapping",
                "required": True,
                "input_type": "multiline_text",
                "rows": 4,
                "max_length": 1200,
                "placeholder": {
                    "tag": "plain_text",
                    "content": "1-2节：8:00-9:35\n3-4=10:00-11:35",
                },
                "label": {"tag": "plain_text", "content": "节次时间对照"},
            },
        ])
    other_missing = missing - {
        "semester_start_date", "period_time_mapping", "actual_time"
    }
    if other_missing:
        labels = {
            "weekday": "上课星期",
            "week_rule": "起止周或单、双周规则",
        }
        detail = "、".join(labels.get(value, "课程详情") for value in sorted(other_missing))
        elements.append({
            "tag": "markdown",
            "content": f"这张图还有无法可靠识别的{detail}，请先用文字补充后再继续。",
        })
    elements.append({
        "tag": "button",
        "name": "course_schedule_import_context_submit",
        "type": "primary",
        "text": {"tag": "plain_text", "content": "生成导入预览"},
        "form_action_type": "submit",
        "behaviors": [{"type": "callback", "value": {
            "mindflow_action": "course_schedule_import_context_submit",
            "version": "3",
            "import_id": str(draft["id"]),
        }}],
    })
    return {
        "schema": "2.0",
        "config": {"update_multi": True, "enable_forward": False},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": "补充课表导入信息"},
        },
        "body": {
            "direction": "vertical",
            "elements": [{
                "tag": "form",
                "name": "mindflow_course_schedule_context",
                "elements": elements,
            }],
        },
    }


def _safe_schedule_text(value: Any) -> str:
    return str(value).replace("<", "＜").replace(">", "＞")[:500]

from app.services.curve_analysis import CurveAnalysis, forecast_model_context


def select_card_key_nodes(analysis: CurveAnalysis, limit: int = 8) -> list[Any]:
    """Keep safety-relevant nodes visible before filling with calendar items."""

    priority = {
        "high_risk_entry": 0,
        "peak": 1,
        "warning_window": 2,
        "recovery": 3,
        "risk_entry": 4,
        "calendar_event": 5,
    }
    ranked = sorted(
        analysis.key_nodes,
        key=lambda node: (priority.get(node.type, 4), node.time),
    )[:max(0, limit)]
    return sorted(ranked, key=lambda node: node.time)


def daily_checkin_card() -> dict[str, Any]:
    """Build the fixed non-clinical check-in form accepted by the callback service."""

    scale_options = [
        {"text": {"tag": "plain_text", "content": str(value)}, "value": str(value)}
        for value in range(11)
    ]
    boolean_options = [
        {"text": {"tag": "plain_text", "content": "是"}, "value": "true"},
        {"text": {"tag": "plain_text", "content": "否"}, "value": "false"},
    ]
    return {
        "schema": "2.0",
        "config": {
            "update_multi": True,
            "enable_forward": False,
        },
        "header": {
            "template": "turquoise",
            "title": {"tag": "plain_text", "content": "每日状态记录"},
        },
        "body": {
            "direction": "vertical",
            "vertical_spacing": "12px",
            "elements": [
                {
                    "tag": "markdown",
                    "content": "请按此刻感受填写。0 表示最低，10 表示最高。",
                },
                {
                    "tag": "form",
                    "name": "mindflow_daily_checkin",
                    "elements": [
                        {
                            "tag": "markdown",
                            "content": "**当前压力**",
                        },
                        {
                            "tag": "select_static",
                            "name": "stress",
                            "required": True,
                            "placeholder": {
                                "tag": "plain_text",
                                "content": "选择 0–10",
                            },
                            "options": scale_options,
                        },
                        {
                            "tag": "markdown",
                            "content": "**当前精力**",
                        },
                        {
                            "tag": "select_static",
                            "name": "energy",
                            "required": True,
                            "placeholder": {
                                "tag": "plain_text",
                                "content": "选择 0–10",
                            },
                            "options": scale_options,
                        },
                        {
                            "tag": "input",
                            "name": "activity",
                            "required": True,
                            "max_length": 120,
                            "placeholder": {
                                "tag": "plain_text",
                                "content": "例如：在图书馆写作业",
                            },
                            "label": {"tag": "plain_text", "content": "正在做什么"},
                        },
                        {
                            "tag": "markdown",
                            "content": "**上次记录后有压力事件吗**",
                        },
                        {
                            "tag": "select_static",
                            "name": "stress_event_since_last",
                            "required": True,
                            "placeholder": {
                                "tag": "plain_text",
                                "content": "请选择",
                            },
                            "options": boolean_options,
                        },
                        {
                            "tag": "markdown",
                            "content": "**该事件仍在持续吗**",
                        },
                        {
                            "tag": "select_static",
                            "name": "event_ongoing",
                            "required": True,
                            "placeholder": {
                                "tag": "plain_text",
                                "content": "请选择",
                            },
                            "options": boolean_options,
                        },
                        {
                            "tag": "button",
                            "name": "submit_checkin",
                            "type": "primary",
                            "text": {"tag": "plain_text", "content": "提交记录"},
                            "form_action_type": "submit",
                            "behaviors": [
                                {
                                    "type": "callback",
                                    "value": {
                                        "mindflow_action": "submit_checkin",
                                        "version": "1",
                                    },
                                }
                            ],
                            "confirm": {
                                "title": {
                                    "tag": "plain_text",
                                    "content": "提交状态记录",
                                },
                                "text": {
                                    "tag": "plain_text",
                                    "content": "确认提交本次记录吗？",
                                },
                            },
                        },
                    ],
                },
                {
                    "tag": "markdown",
                    "text_size": "notation",
                    "content": "用于日常状态建模，不是医学问卷或诊断量表。",
                },
            ],
        },
    }


def daily_review_card(
    *, schedule_id: str, local_date: str, card_version: str = "daily-review-v1"
) -> dict[str, Any]:
    """Fixed end-of-day review form; values are validated again server-side."""

    scale_options = [
        {"text": {"tag": "plain_text", "content": str(value)}, "value": str(value)}
        for value in range(11)
    ]
    period_options = [
        ("凌晨 00:00–06:00", "overnight"),
        ("清晨 06:00–09:00", "early_morning"),
        ("上午 09:00–12:00", "morning"),
        ("中午 12:00–14:00", "noon"),
        ("下午 14:00–18:00", "afternoon"),
        ("晚上 18:00–22:00", "evening"),
        ("夜间 22:00 以后", "late_night"),
        ("不确定", "unknown"),
    ]
    elements: list[dict[str, Any]] = []

    def add_scale(
        name: str, *, prompt: str, guidance: str = "", required: bool = True
    ) -> None:
        description = f"**{prompt}**"
        if guidance:
            description += f"\n{guidance}"
        elements.append({"tag": "markdown", "content": description})
        elements.append({
            "tag": "select_static", "name": name, "required": required,
            "placeholder": {
                "tag": "plain_text",
                "content": "选择 0–10" if required else "选填 0–10",
            },
            "options": scale_options,
        })

    add_scale(
        "start_stress",
        prompt=f"① 回顾 {local_date}：当天早晨刚开始一天时，你的压力有多高？",
        guidance="0 = 完全没有压力　·　5 = 中等压力　·　10 = 已经非常难承受",
    )
    add_scale(
        "start_energy",
        prompt=f"② 回顾 {local_date}：当天早晨的精力怎么样？",
        guidance="0 = 几乎没有精力　·　5 = 一般　·　10 = 精力非常充足",
    )
    add_scale(
        "peak_stress",
        prompt=f"③ 回顾 {local_date}：当天最高压力大约有多高？",
        guidance="0 = 没有明显压力　·　10 = 当天最难承受的程度",
    )
    elements.append({
        "tag": "markdown",
        "content": f"**④ 回顾 {local_date}：当天最高压力大约出现在什么时候？**",
    })
    elements.append({
        "tag": "select_static", "name": "peak_period", "required": True,
        "placeholder": {"tag": "plain_text", "content": "选择大致时段"},
        "options": [
            {"text": {"tag": "plain_text", "content": label}, "value": value}
            for label, value in period_options
        ],
    })
    add_scale(
        "end_stress",
        prompt=f"⑤ 回顾 {local_date}：当天结束时（约晚间/睡前），你的压力有多高？",
        guidance="0 = 完全没有压力　·　10 = 已经非常难承受",
    )
    add_scale(
        "end_energy",
        prompt=f"⑥ 回顾 {local_date}：当天结束时，你还剩多少精力？",
        guidance="0 = 基本耗尽　·　10 = 仍然非常充足",
    )
    elements.append({
        "tag": "markdown",
        "content": "💡 第 ⑤、⑥ 项会用于帮助估计下一天的起始状态。",
    })
    add_scale(
        "energy_consumption",
        prompt=f"⑦ 回顾 {local_date}：当天整体让你感觉被消耗了多少？（选填）",
        guidance=(
            "0 = 几乎没被消耗　·　10 = 非常消耗\n"
            "当前主要用于研究分析，不会直接改变压力或精力曲线。"
        ),
        required=False,
    )
    for name, prompt, placeholder, maximum in (
        (
            "main_stressor",
            f"回顾 {local_date}：当天最主要的压力来自什么？（选填）",
            "例如：考试、连续会议、项目截止、睡眠不足",
            300,
        ),
        (
            "recovery_note",
            f"回顾 {local_date}：当天什么事情让你稍微恢复了一些？（选填）",
            "例如：午休、散步、运动、和朋友聊天",
            300,
        ),
        (
            "free_text",
            "还有什么会帮助我们理解回顾当天的状态？（选填）",
            "可留空",
            1000,
        ),
    ):
        elements.append({"tag": "markdown", "content": f"**{prompt}**"})
        input_element = {
            "tag": "input", "name": name, "required": False, "max_length": maximum,
            "placeholder": {"tag": "plain_text", "content": placeholder},
            "label": {"tag": "plain_text", "content": prompt},
        }
        if name == "free_text":
            input_element.update({"input_type": "multiline_text", "rows": 3})
        elements.append(input_element)
    elements.append({
        "tag": "markdown",
        "content": "以上文字主要用于回顾和研究分析，目前不会直接改变压力曲线数值。",
    })
    elements.append({
        "tag": "button", "name": "daily_review_submit",
        "type": "primary",
        "text": {"tag": "plain_text", "content": "提交每日回顾"},
        "form_action_type": "submit",
        "behaviors": [
            {
                "type": "callback",
                "value": {
                    "mindflow_action": "daily_review_submit", "version": "1",
                    "schedule_id": schedule_id, "local_date": local_date,
                    "card_version": card_version,
                },
            }
        ],
    })
    return {
        "schema": "2.0",
        "config": {"update_multi": True, "enable_forward": False},
        "header": {"template": "purple", "title": {"tag": "plain_text", "content": "MindFlow 每日回顾"}},
        "body": {
            "direction": "vertical",
            "vertical_spacing": "12px",
            "elements": [
                {
                    "tag": "markdown",
                    "content": (
                        f"回顾 **{local_date}**。这是回顾反馈，不会改写当天原始预测。\n"
                        "如果这是次日补填，请回忆上方标注日期当天的状态，不要填写此刻状态。"
                    ),
                },
                {"tag": "form", "name": "mindflow_daily_review", "elements": elements},
                {
                    "tag": "markdown",
                    "text_size": "notation",
                    "content": "用于日常回顾与建模，不是医学诊断。",
                },
            ],
        },
    }


def care_intervention_card(
    *,
    intervention_id: str,
    message: str,
    actions: list[str],
) -> dict[str, Any]:
    """Reviewed proactive-care card with an allowlisted action envelope."""

    labels = {
        "ack": ("知道了", "primary"),
        "snooze_30": ("30 分钟后提醒", "default"),
        "mute_today": ("今天不再提醒", "default"),
        "helpful": ("有帮助", "default"),
        "not_relevant": ("不太相关", "default"),
        "disable_type": ("不想收到这类提醒", "default"),
    }
    buttons = []
    for action in actions:
        if action not in labels:
            continue
        label, button_type = labels[action]
        buttons.append(
            {
                "tag": "button",
                "type": button_type,
                "text": {"tag": "plain_text", "content": label},
                "behaviors": [
                    {
                        "type": "callback",
                        "value": {
                            "mindflow_action": f"care_{action}",
                            "version": "1",
                            "intervention_id": str(intervention_id),
                        },
                    }
                ],
            }
        )
    button_rows = []
    for offset in range(0, len(buttons), 2):
        button_rows.append(
            {
                "tag": "column_set",
                "flex_mode": "bisect",
                "horizontal_spacing": "8px",
                "columns": [
                    {
                        "tag": "column",
                        "width": "weighted",
                        "weight": 1,
                        "elements": [button],
                    }
                    for button in buttons[offset:offset + 2]
                ],
            }
        )
    return {
        "schema": "2.0",
        "config": {
            "update_multi": True,
            "width_mode": "fill",
            "enable_forward": False,
            "summary": {"content": "MindFlow 关怀提醒"},
        },
        "header": {
            "template": "turquoise",
            "title": {"tag": "plain_text", "content": "MindFlow 关怀提醒"},
        },
        "body": {
            "direction": "vertical",
            "elements": [
                {"tag": "markdown", "content": str(message)[:1000]},
                *button_rows,
                {
                    "tag": "markdown",
                    "text_size": "notation",
                    "content": "这是非临床的趋势提醒；你可以忽略、延后或关闭这一类提醒。",
                },
            ],
        },
    }


def care_intervention_result_card(
    *, message: str, result_text: str
) -> dict[str, Any]:
    """Replace an acted-on Care card with a final state and no action buttons."""

    return {
        "schema": "2.0",
        "config": {
            "update_multi": True,
            "width_mode": "fill",
            "enable_forward": False,
            "summary": {"content": "MindFlow 关怀提醒"},
        },
        "header": {
            "template": "turquoise",
            "title": {"tag": "plain_text", "content": "MindFlow 关怀提醒"},
        },
        "body": {
            "direction": "vertical",
            "elements": [
                {"tag": "markdown", "content": str(message)[:1000]},
                {"tag": "markdown", "content": str(result_text)[:500]},
            ],
        },
    }


def card_action_result_card(*, message: str) -> dict[str, Any]:
    """Generic final card used when a successful form action has no own card."""

    return {
        "schema": "2.0",
        "config": {
            "update_multi": True,
            "width_mode": "fill",
            "enable_forward": False,
            "summary": {"content": "MindFlow"},
        },
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": "MindFlow"},
        },
        "body": {
            "direction": "vertical",
            "elements": [
                {"tag": "markdown", "content": str(message or "已提交")[:1000]}
            ],
        },
    }


def calendar_delete_confirmation_card(
    event: dict[str, Any],
    *,
    timezone_name: str = "Asia/Shanghai",
) -> dict[str, Any]:
    """Fixed confirmation card for one backend-resolved Calendar event."""

    event_id = str(event.get("id") or "").strip()
    if not event_id or len(event_id) > 256:
        raise ValueError("calendar event id is invalid")
    summary = str(event.get("summary") or "未命名日程")[:200]
    date_range = _format_calendar_datetime_range(
        event.get("start_time"),
        event.get("end_time"),
        timezone_name=timezone_name,
    )
    return {
        "schema": "2.0",
        "config": {
            "update_multi": True,
            "width_mode": "fill",
            "enable_forward": False,
            "summary": {"content": "确认删除日程"},
        },
        "header": {
            "template": "red",
            "title": {"tag": "plain_text", "content": "确认删除日程"},
        },
        "body": {
            "direction": "vertical",
            "elements": [
                {
                    "tag": "markdown",
                    "content": (
                        f"**{summary}**\n{date_range}\n\n"
                        "删除后无法恢复，请确认是否继续。"
                    ),
                },
                {
                    "tag": "button",
                    "type": "danger",
                    "text": {"tag": "plain_text", "content": "确认删除"},
                    "behaviors": [{
                        "type": "callback",
                        "value": {
                            "mindflow_action": "calendar_delete_confirm",
                            "version": "1",
                            "event_id": event_id,
                        },
                    }],
                },
                {
                    "tag": "button",
                    "type": "default",
                    "text": {"tag": "plain_text", "content": "取消"},
                    "behaviors": [{
                        "type": "callback",
                        "value": {
                            "mindflow_action": "calendar_delete_cancel",
                            "version": "1",
                        },
                    }],
                },
            ],
        },
    }


def calendar_mutation_plan_confirmation_card(
    plan: dict[str, Any],
    *,
    timezone_name: str = "Asia/Shanghai",
) -> dict[str, Any]:
    """One fixed card for an immutable backend-owned batch mutation plan."""

    plan_id = str(plan.get("id") or "").strip()
    operation = str(plan.get("operation") or "").strip()
    items = [dict(item) for item in list(plan.get("items") or [])]
    if not plan_id or len(plan_id) > 64:
        raise ValueError("calendar mutation plan id is invalid")
    if operation not in {"create", "update", "delete"} or not 1 <= len(items) <= 20:
        raise ValueError("calendar mutation plan is invalid")
    verb = {"create": "添加", "update": "修改", "delete": "删除"}[operation]
    ledger_by_index: dict[int, dict[str, Any]] = {}
    if operation in {"create", "update"}:
        ledger_items = [dict(item) for item in list(plan.get("ledger_items") or [])]
        for ledger_item in ledger_items:
            try:
                item_index = int(ledger_item.get("item_index"))
            except (TypeError, ValueError) as exc:
                raise ValueError("calendar plan ledger index is invalid") from exc
            item_id = str(ledger_item.get("id") or "").strip()
            if (
                not item_id
                or item_index in ledger_by_index
                or item_index < 0
                or item_index >= len(items)
            ):
                raise ValueError("calendar plan aggregate and ledger are inconsistent")
            if dict(ledger_item.get("payload") or {}) != items[item_index]:
                raise ValueError("calendar plan aggregate and ledger are inconsistent")
            ledger_by_index[item_index] = ledger_item
        if len(ledger_by_index) != len(items):
            raise ValueError("calendar plan aggregate and ledger are inconsistent")

    context = dict(plan.get("presentation_context") or {})
    if operation == "update" and context.get("kind") == "course_series_update":
        return _course_series_update_confirmation_card(
            plan,
            items,
            context,
            timezone_name=timezone_name,
        )

    elements: list[dict[str, Any]] = []
    for item_offset, item in enumerate(items):
        display_index = item_offset + 1
        summary = str(item.get("summary") or "未命名日程")[:80]
        date_range = _format_calendar_datetime_range(
            item.get("start_time"),
            item.get("end_time"),
            timezone_name=timezone_name,
        )
        elements.append({
            "tag": "markdown",
            "content": f"{display_index}. **{summary}**\n   {date_range}",
        })
        if operation in {"create", "update"}:
            elements.append({
                "tag": "button",
                "type": "default",
                "text": {
                    "tag": "plain_text",
                    "content": "修改信息",
                },
                "behaviors": [{
                    "type": "callback",
                    "value": {
                        "mindflow_action": "calendar_mutation_plan_item_edit_open",
                        "version": "1",
                        "plan_id": plan_id,
                        "item_id": str(ledger_by_index[item_offset]["id"]),
                    },
                }],
            })
    elements.extend([
        {
            "tag": "button",
            "type": "danger" if operation == "delete" else "primary",
            "text": {
                "tag": "plain_text",
                "content": f"确认{verb}",
            },
            "behaviors": [{
                "type": "callback",
                "value": {
                    "mindflow_action": "calendar_mutation_plan_confirm",
                    "version": "1",
                    "plan_id": plan_id,
                },
            }],
        },
        {
            "tag": "button",
            "type": "default",
            "text": {"tag": "plain_text", "content": "取消"},
            "behaviors": [{
                "type": "callback",
                "value": {
                    "mindflow_action": "calendar_mutation_plan_cancel",
                    "version": "1",
                    "plan_id": plan_id,
                },
            }],
        },
    ])
    return {
        "schema": "2.0",
        "config": {
            "update_multi": True,
            "width_mode": "fill",
            "enable_forward": False,
            "summary": {"content": f"确认{verb}日程"},
        },
        "header": {
            "template": "red" if operation == "delete" else "blue",
            "title": {
                "tag": "plain_text",
                "content": f"确认{verb}以下 {len(items)} 个日程",
            },
        },
        "body": {
            "direction": "vertical",
            "elements": elements,
        },
    }


def reminder_proposal_confirmation_card(
    proposal: dict[str, Any],
    *,
    timezone_name: str = "Asia/Shanghai",
) -> dict[str, Any]:
    """Fixed review card whose callback carries only a backend proposal id."""

    proposal_id = str(proposal.get("id") or "").strip()
    operation = str(proposal.get("operation") or "").strip()
    payload = dict(proposal.get("payload") or {})
    if not proposal_id or len(proposal_id) > 64 or operation not in {"create", "cancel"}:
        raise ValueError("reminder proposal is invalid")
    message = str(payload.get("message") or "").strip()[:500]
    raw_time = str(payload.get("remind_at") or "").strip()
    try:
        parsed = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        display_time = parsed.astimezone(ZoneInfo(timezone_name)).strftime(
            "%Y-%m-%d %H:%M"
        )
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("reminder proposal time is invalid") from exc
    recurrence = {
        "none": "仅一次",
        "daily": "每天",
        "weekly": "每周",
    }.get(str(payload.get("recurrence_type") or ""))
    if recurrence is None or not message:
        raise ValueError("reminder proposal payload is invalid")
    cancelling = operation == "cancel"
    title = "确认取消提醒" if cancelling else "确认创建提醒"
    confirm_label = "确认取消" if cancelling else "确认提醒"
    return {
        "schema": "2.0",
        "config": {
            "update_multi": True,
            "width_mode": "fill",
            "enable_forward": False,
            "summary": {"content": title},
        },
        "header": {
            "template": "orange" if cancelling else "blue",
            "title": {"tag": "plain_text", "content": title},
        },
        "body": {
            "direction": "vertical",
            "elements": [
                {
                    "tag": "markdown",
                    "content": (
                        f"**{message}**\n时间：{display_time}\n重复：{recurrence}\n\n"
                        + (
                            "确认后这条提醒将停止。"
                            if cancelling
                            else "确认前不会保存或发送这条提醒。"
                        )
                    ),
                },
                {
                    "tag": "button",
                    "type": "danger" if cancelling else "primary",
                    "text": {"tag": "plain_text", "content": confirm_label},
                    "behaviors": [{
                        "type": "callback",
                        "value": {
                            "mindflow_action": "reminder_proposal_confirm",
                            "version": "1",
                            "proposal_id": proposal_id,
                        },
                    }],
                },
                {
                    "tag": "button",
                    "type": "default",
                    "text": {"tag": "plain_text", "content": "取消"},
                    "behaviors": [{
                        "type": "callback",
                        "value": {
                            "mindflow_action": "reminder_proposal_cancel",
                            "version": "1",
                            "proposal_id": proposal_id,
                        },
                    }],
                },
            ],
        },
    }


def _course_series_update_confirmation_card(
    plan: dict[str, Any],
    items: list[dict[str, Any]],
    context: dict[str, Any],
    *,
    timezone_name: str,
) -> dict[str, Any]:
    plan_id = str(plan["id"])
    scope_label = {
        "single_occurrence": "仅这一节",
        "current_semester_remainder": "从当前场次到本学期结束",
        "entire_series": "本系列全部场次（含过去）",
    }.get(str(context.get("scope") or ""), "已解析范围")
    first = items[0]
    previous = dict(first.get("previous") or {})
    proposed = dict(first.get("proposed") or first)
    old_start = _calendar_datetime(previous.get("start_time") or first["start_time"])
    old_end = _calendar_datetime(previous.get("end_time") or first["end_time"])
    new_start = _calendar_datetime(proposed.get("start_time") or first["start_time"])
    new_end = _calendar_datetime(proposed.get("end_time") or first["end_time"])
    display_timezone = ZoneInfo(timezone_name)
    changes = dict(context.get("changes") or {})
    lines = [
        f"**课程：** {str(context.get('display_name') or first.get('summary') or '未命名课程')[:200]}",
        f"**范围：** {scope_label}",
        f"**场次：** {len(items)} 节",
    ]
    if "summary" in changes:
        lines.append(
            f"**名称：** {str(previous.get('summary') or '未命名课程')[:80]} → "
            f"{str(proposed.get('summary') or first.get('summary'))[:80]}"
        )
    if "start_clock" in changes or new_start != old_start:
        lines.append(
            "**开始时间：** "
            f"{old_start.astimezone(display_timezone).strftime('%H:%M')} → "
            f"{new_start.astimezone(display_timezone).strftime('%H:%M')}"
        )
    if "end_clock" in changes or new_end != old_end:
        lines.append(
            "**结束时间：** "
            f"{old_end.astimezone(display_timezone).strftime('%H:%M')} → "
            f"{new_end.astimezone(display_timezone).strftime('%H:%M')}"
        )
    elements: list[dict[str, Any]] = [
        {"tag": "markdown", "content": "\n".join(lines)},
        {
            "tag": "button",
            "type": "default",
            "text": {"tag": "plain_text", "content": "修改信息"},
            "behaviors": [{
                "type": "callback",
                "value": {
                    "mindflow_action": "calendar_course_series_edit_open",
                    "version": "1",
                    "plan_id": plan_id,
                },
            }],
        },
        {
            "tag": "button",
            "type": "default",
            "text": {"tag": "plain_text", "content": "查看场次"},
            "behaviors": [{
                "type": "callback",
                "value": {
                    "mindflow_action": "calendar_course_series_occurrences_view",
                    "version": "1",
                    "plan_id": plan_id,
                },
            }],
        },
        {
            "tag": "button",
            "type": "primary",
            "text": {"tag": "plain_text", "content": "确认修改"},
            "behaviors": [{
                "type": "callback",
                "value": {
                    "mindflow_action": "calendar_mutation_plan_confirm",
                    "version": "1",
                    "plan_id": plan_id,
                },
            }],
        },
        {
            "tag": "button",
            "type": "default",
            "text": {"tag": "plain_text", "content": "取消"},
            "behaviors": [{
                "type": "callback",
                "value": {
                    "mindflow_action": "calendar_mutation_plan_cancel",
                    "version": "1",
                    "plan_id": plan_id,
                },
            }],
        },
    ]
    return {
        "schema": "2.0",
        "config": {
            "update_multi": True,
            "width_mode": "fill",
            "enable_forward": False,
            "summary": {"content": "确认批量修改课程"},
        },
        "header": {
            "template": "blue",
            "title": {
                "tag": "plain_text",
                "content": f"确认修改 {len(items)} 节课程",
            },
        },
        "body": {"direction": "vertical", "elements": elements},
    }


def calendar_course_series_occurrences_card(
    plan: dict[str, Any], *, timezone_name: str = "Asia/Shanghai"
) -> dict[str, Any]:
    plan_id = str(plan.get("id") or "")
    items = [dict(item) for item in list(plan.get("items") or [])]
    if not plan_id or not items:
        raise ValueError("calendar course series plan is invalid")
    elements = [
        {
            "tag": "markdown",
            "content": (
                f"{index}. **{str(item.get('summary') or '未命名课程')[:80]}**\n"
                f"   {_format_calendar_datetime_range(item.get('start_time'), item.get('end_time'), timezone_name=timezone_name)}"
            ),
        }
        for index, item in enumerate(items, start=1)
    ]
    elements.append({
        "tag": "button",
        "type": "default",
        "text": {"tag": "plain_text", "content": "返回确认"},
        "behaviors": [{
            "type": "callback",
            "value": {
                "mindflow_action": "calendar_mutation_plan_view",
                "version": "1",
                "plan_id": plan_id,
            },
        }],
    })
    return {
        "schema": "2.0",
        "config": {"update_multi": True, "width_mode": "fill", "enable_forward": False},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": f"将修改的 {len(items)} 节课程"},
        },
        "body": {"direction": "vertical", "elements": elements},
    }


def calendar_course_series_edit_card(
    plan: dict[str, Any], *, timezone_name: str = "Asia/Shanghai"
) -> dict[str, Any]:
    plan_id = str(plan.get("id") or "")
    items = [dict(item) for item in list(plan.get("items") or [])]
    context = dict(plan.get("presentation_context") or {})
    if (
        not plan_id
        or not items
        or str(plan.get("status") or "") != "awaiting_confirmation"
        or context.get("kind") != "course_series_update"
    ):
        raise ValueError("calendar course series plan is not editable")
    first = items[0]
    display_timezone = ZoneInfo(timezone_name)
    local_start = _calendar_datetime(first.get("start_time")).astimezone(display_timezone)
    local_end = _calendar_datetime(first.get("end_time")).astimezone(display_timezone)
    summary = str(first.get("summary") or "未命名课程")[:200]
    return {
        "schema": "2.0",
        "config": {"update_multi": True, "width_mode": "fill", "enable_forward": False},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": "统一修改课程信息"},
        },
        "body": {
            "direction": "vertical",
            "elements": [
                {
                    "tag": "form",
                    "name": "mindflow_calendar_course_series_edit",
                    "elements": [
                        {
                            "tag": "markdown",
                            "content": f"保存后会统一更新待确认的 {len(items)} 节课程，不会立即写入日历。",
                        },
                        {
                            "tag": "input",
                            "name": "summary",
                            "default_value": summary,
                            "required": True,
                            "max_length": 200,
                            "label": {"tag": "plain_text", "content": "课程名称"},
                        },
                        *_clock_form_fields(
                            local_start.strftime("%H:%M"),
                            local_end.strftime("%H:%M"),
                        ),
                        {
                            "tag": "button",
                            "name": "calendar_course_series_edit_submit",
                            "type": "primary",
                            "text": {"tag": "plain_text", "content": "保存修改"},
                            "form_action_type": "submit",
                            "behaviors": [{
                                "type": "callback",
                                "value": {
                                    "mindflow_action": "calendar_course_series_edit_submit",
                                    "version": "1",
                                    "plan_id": plan_id,
                                },
                            }],
                        },
                    ],
                },
                {
                    "tag": "button",
                    "type": "default",
                    "text": {"tag": "plain_text", "content": "返回确认"},
                    "behaviors": [{
                        "type": "callback",
                        "value": {
                            "mindflow_action": "calendar_mutation_plan_view",
                            "version": "1",
                            "plan_id": plan_id,
                        },
                    }],
                },
            ],
        },
    }


def _calendar_mutation_plan_item_edit_card(
    plan: dict[str, Any],
    item_id: str,
    *,
    timezone_name: str = "Asia/Shanghai",
    submit_action: str,
    form_name: str,
) -> dict[str, Any]:
    """Fixed summary/time form bound to one pending plan ledger item."""

    plan_id = str(plan.get("id") or "").strip()
    if (
        not plan_id
        or str(plan.get("operation") or "") not in {"create", "update"}
        or str(plan.get("status") or "") != "awaiting_confirmation"
    ):
        raise ValueError("calendar mutation plan is not editable")
    target_id = str(item_id or "").strip()
    ledger_item = next(
        (
            dict(value)
            for value in list(plan.get("ledger_items") or [])
            if str(value.get("id") or "") == target_id
        ),
        None,
    )
    if ledger_item is None or str(ledger_item.get("status") or "") != "pending":
        raise LookupError("calendar mutation plan item not found")
    try:
        item_index = int(ledger_item.get("item_index"))
        item = dict(list(plan.get("items") or [])[item_index])
    except (IndexError, TypeError, ValueError) as exc:
        raise ValueError("calendar plan aggregate and ledger are inconsistent") from exc
    if dict(ledger_item.get("payload") or {}) != item:
        raise ValueError("calendar plan aggregate and ledger are inconsistent")

    display_timezone = ZoneInfo(timezone_name)
    local_start = _calendar_datetime(item.get("start_time")).astimezone(
        display_timezone
    )
    local_end = _calendar_datetime(item.get("end_time")).astimezone(display_timezone)
    date_range = _format_calendar_datetime_range(
        local_start, local_end, timezone_name=timezone_name
    )
    summary = str(item.get("summary") or "未命名日程")[:200]
    form_elements = [
        {
            "tag": "markdown",
            "content": (
                f"当前信息：**{summary}**\n{date_range}\n"
                "保存只会更新待确认内容，不会立即写入日历。"
            ),
        },
        {
            "tag": "input",
            "name": "summary",
            "default_value": summary,
            "required": True,
            "max_length": 200,
            "label": {"tag": "plain_text", "content": "日程名称"},
        },
        *_clock_form_fields(
            local_start.strftime("%H:%M"), local_end.strftime("%H:%M")
        ),
        {
            "tag": "button",
            "name": submit_action,
            "type": "primary",
            "text": {"tag": "plain_text", "content": "保存修改"},
            "form_action_type": "submit",
            "behaviors": [{
                "type": "callback",
                "value": {
                    "mindflow_action": submit_action,
                    "version": "1",
                    "plan_id": plan_id,
                    "item_id": target_id,
                },
            }],
        },
    ]
    return {
        "schema": "2.0",
        "config": {
            "update_multi": True,
            "width_mode": "fill",
            "enable_forward": False,
            "summary": {"content": "修改日程信息"},
        },
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": "修改信息"},
        },
        "body": {
            "direction": "vertical",
            "elements": [
                {
                    "tag": "form",
                    "name": form_name,
                    "elements": form_elements,
                },
                {
                    "tag": "button",
                    "type": "default",
                    "text": {"tag": "plain_text", "content": "返回确认"},
                    "behaviors": [{
                        "type": "callback",
                        "value": {
                            "mindflow_action": "calendar_mutation_plan_view",
                            "version": "1",
                            "plan_id": plan_id,
                        },
                    }],
                },
            ],
        },
    }


def calendar_mutation_plan_item_edit_card(
    plan: dict[str, Any],
    item_id: str,
    *,
    timezone_name: str = "Asia/Shanghai",
) -> dict[str, Any]:
    return _calendar_mutation_plan_item_edit_card(
        plan,
        item_id,
        timezone_name=timezone_name,
        submit_action="calendar_mutation_plan_item_edit_submit",
        form_name="mindflow_calendar_mutation_plan_item_edit",
    )


def calendar_mutation_plan_item_time_card(
    plan: dict[str, Any],
    item_id: str,
    *,
    timezone_name: str = "Asia/Shanghai",
) -> dict[str, Any]:
    """Compatibility builder for callbacks from already-delivered cards."""

    return _calendar_mutation_plan_item_edit_card(
        plan,
        item_id,
        timezone_name=timezone_name,
        submit_action="calendar_mutation_plan_item_time_submit",
        form_name="mindflow_calendar_mutation_plan_item_time",
    )


def pressure_curve_card(
    analysis: CurveAnalysis,
    *,
    image_key: str,
    local_date: str,
    model_output: dict[str, Any] | None = None,
    requested_date_is_today: bool = True,
) -> dict[str, Any]:
    """Build the image/key-node card from the same authoritative analysis."""

    normalized_key = str(image_key or "").strip()
    if not normalized_key:
        raise ValueError("pressure curve image_key is required")
    nodes = select_card_key_nodes(analysis, 8)
    node_text = "\n".join(
        f"• **{node.time}** {node.text}" for node in nodes
    ) or "• 暂无显著风险节点"
    model = forecast_model_context(model_output)
    if model.has_dynamic_vitality:
        vitality = (
            f"{analysis.current_vitality:g}/10"
            if analysis.current_vitality is not None
            else "暂无"
        )
        summary = (
            f"**当前压力：** {analysis.current_stress:g}/10　"
            f"**当前活力：** {vitality}\n"
            f"**{'今日' if requested_date_is_today else '当日'}峰值：** "
            f"{analysis.peak_stress:g}/10（{analysis.peak_stress_time}）"
        )
        title = (
            "今日压力与活力趋势"
            if requested_date_is_today
            else f"{str(local_date)[:10]} 压力与活力趋势"
        )
    else:
        summary = (
            f"**当前压力：** {analysis.current_stress:g}/10\n"
            f"**{'今日' if requested_date_is_today else '当日'}峰值：** "
            f"{analysis.peak_stress:g}/10（{analysis.peak_stress_time}）"
        )
        title = (
            "今日压力趋势"
            if requested_date_is_today
            else f"{str(local_date)[:10]} 压力趋势"
        )

    return {
        "schema": "2.0",
        "config": {
            "width_mode": "fill",
            "enable_forward": False,
            "summary": {"content": title},
        },
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": title},
            "subtitle": {"tag": "plain_text", "content": str(local_date)[:10]},
        },
        "body": {
            "direction": "vertical",
            "elements": [
                {
                    "tag": "img",
                    "img_key": normalized_key,
                    "alt": {"tag": "plain_text", "content": title},
                    "mode": "fit_horizontal",
                    "preview": True,
                },
                {"tag": "markdown", "content": summary},
                {"tag": "markdown", "content": "**关键时段**\n" + node_text},
                {
                    "tag": "button",
                    "type": "primary",
                    "text": {"tag": "plain_text", "content": "记录当前状态"},
                    "behaviors": [{
                        "type": "callback",
                        "value": {
                            "mindflow_action": "request_checkin",
                            "version": "1",
                        },
                    }],
                },
                {
                    "tag": "button",
                    "type": "default",
                    "text": {
                        "tag": "plain_text",
                        "content": (
                            "查看今日日程"
                            if requested_date_is_today
                            else "查看当日日程"
                        ),
                    },
                    "behaviors": [{
                        "type": "callback",
                        "value": {
                            "mindflow_action": "view_calendar_date",
                            "version": "1",
                            "local_date": str(local_date)[:10],
                        },
                    }],
                },
                {
                    "tag": "markdown",
                    "text_size": "notation",
                    "content": "图中内容为模型预测，仅供日常状态参考，不代表已观察事实或医疗判断。",
                },
            ],
        },
    }


def today_calendar_card(
    events: list[dict[str, Any]],
    *,
    local_date: str,
    requested_date_is_today: bool = True,
    timezone_name: str = "Asia/Shanghai",
) -> dict[str, Any]:
    lines = []
    for event in events[:20]:
        raw_start = event.get("start_time")
        label = (
            _calendar_datetime(raw_start)
            .astimezone(ZoneInfo(timezone_name))
            .strftime("%H:%M")
            if raw_start
            else "全天"
        )
        summary = str(event.get("summary") or "未命名日程")[:80]
        lines.append(f"• **{label}** {summary}")
    day_label = "今日" if requested_date_is_today else "当日"
    content = "\n".join(lines) if lines else f"{day_label}暂无日程。"
    return {
        "schema": "2.0",
        "config": {"width_mode": "fill", "enable_forward": False},
        "header": {
            "template": "turquoise",
            "title": {"tag": "plain_text", "content": f"{day_label}日程"},
            "subtitle": {"tag": "plain_text", "content": str(local_date)[:10]},
        },
        "body": {
            "elements": [
                {"tag": "markdown", "content": content},
                {
                    "tag": "button",
                    "type": "primary",
                    "text": {"tag": "plain_text", "content": "记录当前状态"},
                    "behaviors": [{
                        "type": "callback",
                        "value": {
                            "mindflow_action": "request_checkin",
                            "version": "1",
                        },
                    }],
                },
            ]
        },
    }


_CONSENT_CARD_VERSION = "1"


def _consent_button(
    action_name: str,
    text: str,
    *,
    primary: bool = False,
    bind_consent_version: bool = False,
    feature_key: str | None = None,
) -> dict[str, Any]:
    """Consent callback values carry only action/schema versions.

    ``version`` is the card action schema version; ``consent_version`` is the
    disclosure version the user actually saw, so a stale card can never grant
    a newer consent. No participant ID, image key, or open ID: the backend
    binds the action to the clicking participant through the authenticated
    card callback.
    """

    from app.services.consent_service import EXTERNAL_LLM_CONSENT_VERSION

    value: dict[str, Any] = {
        "mindflow_action": action_name,
        "version": _CONSENT_CARD_VERSION,
    }
    if bind_consent_version:
        value["consent_version"] = EXTERNAL_LLM_CONSENT_VERSION
    if feature_key is not None:
        value["feature_key"] = feature_key
    return {
        "tag": "button",
        "type": "primary" if primary else "default",
        "text": {"tag": "plain_text", "content": text},
        "behaviors": [{"type": "callback", "value": value}],
    }


def _consent_card_shell(
    title: str, content: str, elements: list[dict[str, Any]]
) -> dict[str, Any]:
    elements.insert(0, {"tag": "markdown", "content": content})
    return {
        "schema": "2.0",
        "config": {"update_multi": True, "enable_forward": False},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": title},
        },
        "body": {"direction": "vertical", "elements": elements},
    }


def _consent_navigation_buttons() -> list[dict[str, Any]]:
    return [
        _consent_button(
            "feature_open", "返回「数据与隐私」", feature_key="data_privacy"
        ),
        _consent_button("feature_back", "返回全部功能", feature_key="overview"),
    ]


def external_llm_consent_card() -> dict[str, Any]:
    """Fixed user-consent prompt; one consent covers conversation and images."""

    content = (
        "**开启外部 AI 处理**\n\n"
        "为了回复你的消息、识别你主动发送的图片，以及在需要时理解你的日程内容，"
        "完成这些功能所需的最少内容会发送给外部 AI 模型处理。\n\n"
        "MindFlow 不会把你的账号标识、绑定码等身份信息作为提示内容发送给模型。\n\n"
        "你可以随时在“数据与隐私”中关闭这项能力。"
    )
    elements = [
        _consent_button(
            "external_llm_consent_accept",
            "同意并开启",
            primary=True,
            bind_consent_version=True,
        ),
        _consent_button("external_llm_consent_decline", "暂不使用"),
        _consent_button("external_llm_consent_details_open", "了解详情"),
    ] + _consent_navigation_buttons()
    return _consent_card_shell("外部 AI 处理", content, elements)


def external_llm_consent_details_card() -> dict[str, Any]:
    content = (
        "**外部 AI 处理的数据范围**\n\n"
        "可能发送：\n"
        "- 你主动发送、需要 AI 回复的文字；\n"
        "- 你主动发送并要求识别的图片内容；\n"
        "- 为压力建模理解日程语义时所需的日程标题、描述等最少必要文本。\n\n"
        "不会作为提示内容发送：\n"
        "- 飞书 open_id；\n"
        "- participant_id / participant_code；\n"
        "- 绑定码；\n"
        "- Token / Secret 等凭据。\n\n"
        "用途：仅用于完成对应的回复、图片识别或日程语义分析。\n\n"
        "关闭：关闭后，新的外部 AI 处理立即停止；已保存的本地记录、日历授权和"
        "既有压力数据不会因此被删除。\n\n"
        "数据保存与研究用途：以项目正式知情同意文本为准。"
    )
    elements = [
        _consent_button(
            "external_llm_consent_accept",
            "同意并开启",
            primary=True,
            bind_consent_version=True,
        ),
        _consent_button("external_llm_consent_prompt_open", "返回外部 AI 设置"),
    ] + _consent_navigation_buttons()
    return _consent_card_shell("外部 AI 处理 · 数据范围", content, elements)


def external_llm_consent_status_card(status: dict[str, Any]) -> dict[str, Any]:
    """Per-participant consent state; buttons follow the current state."""

    active = bool(status.get("active"))
    if active:
        state_line = "状态：已开启"
        actions = [
            _consent_button("external_llm_consent_revoke", "关闭外部 AI 处理"),
        ]
    else:
        state_line = "状态：已关闭"
        actions = [
            _consent_button(
                "external_llm_consent_accept",
                "重新开启",
                primary=True,
                bind_consent_version=True,
            ),
        ]
    version = str(status.get("consent_version") or "")
    if version:
        state_line += f"（同意版本 v{version}）"
    content = (
        "**外部 AI 处理**\n\n"
        f"{state_line}\n\n"
        "关闭后，对话和图片不再交给外部 AI 模型处理；"
        "已保存的记录、本地日历和压力功能不受影响。"
    )
    elements = actions + [
        _consent_button("external_llm_consent_details_open", "了解数据范围"),
    ] + _consent_navigation_buttons()
    return _consent_card_shell("数据与隐私 · 外部 AI 处理", content, elements)


def morning_brief_settings_card(preferences: dict[str, Any]) -> dict[str, Any]:
    """Fixed opt-in settings; time is selected rather than typed."""

    enabled = bool(preferences.get("morning_brief_enabled", False))
    current = str(preferences.get("morning_brief_local_time") or "08:00")
    options = [
        {"text": {"tag": "plain_text", "content": value}, "value": value}
        for value in ("07:00", "07:30", "08:00", "08:30", "09:00")
    ]
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": "早报设置"},
        },
        "elements": [
            {"tag": "markdown", "content": (
                f"状态：{'已开启' if enabled else '已关闭'}\n\n发送时间：{current}\n\n"
                "早报只包含今日日程、你创建的提醒事项和一句固定轻提示，不包含压力预测。"
            )},
            {
                "tag": "form", "name": "morning_brief_settings",
                "elements": [
                    {
                        "tag": "select_static", "name": "morning_brief_local_time",
                        "placeholder": {"tag": "plain_text", "content": "选择发送时间"},
                        "initial_option": current, "options": options,
                    },
                    {
                        "tag": "button", "name": "morning_brief_settings_submit",
                        "text": {"tag": "plain_text", "content": "保存时间"},
                        "type": "primary", "action_type": "form_submit",
                        "value": {"mindflow_action": "morning_brief_time_update", "version": "1"},
                    },
                ],
            },
            {
                "tag": "action", "actions": [
                    {
                        "tag": "button", "text": {"tag": "plain_text", "content": "关闭" if enabled else "开启"},
                        "type": "primary", "value": {
                            "mindflow_action": "morning_brief_toggle", "version": "1", "enabled": not enabled,
                        },
                    },
                    {
                        "tag": "button", "text": {"tag": "plain_text", "content": "暂停一周"},
                        "value": {"mindflow_action": "morning_brief_pause_week", "version": "1"},
                    },
                ],
            },
        ],
    }


def memory_center_card(memories: list[dict[str, Any]]) -> dict[str, Any]:
    elements: list[dict[str, Any]] = [{
        "tag": "markdown",
        "content": "这里仅显示你明确要求 MindFlow 记住的内容。你可以逐条删除或全部清空。",
    }]
    for item in memories[:50]:
        elements.append({
            "tag": "column_set", "flex_mode": "stretch", "columns": [
                {"tag": "column", "width": "weighted", "weight": 4, "elements": [{
                    "tag": "markdown", "content": f"**{str(item.get('memory_type') or 'memory')}**\n{str(item.get('content') or '')[:500]}",
                }]},
                {"tag": "column", "width": "weighted", "weight": 1, "elements": [{
                    "tag": "button", "text": {"tag": "plain_text", "content": "查看"},
                    "value": {"mindflow_action": "memory_detail_open", "version": "1", "memory_id": str(item.get("id") or "")},
                }]},
            ],
        })
    if not memories:
        elements.append({"tag": "note", "elements": [{"tag": "plain_text", "content": "目前没有长期记忆。"}]})
    elements.append({
        "tag": "action", "actions": [{
            "tag": "button", "text": {"tag": "plain_text", "content": "清空全部记忆"},
            "type": "danger", "value": {"mindflow_action": "memory_clear_prompt", "version": "1"},
        }],
    })
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": "blue", "title": {"tag": "plain_text", "content": "Memory Center"}},
        "elements": elements,
    }


def memory_detail_card(memory: dict[str, Any]) -> dict[str, Any]:
    return {
        "header": {"template": "blue", "title": {"tag": "plain_text", "content": "记忆详情"}},
        "elements": [
            {"tag": "markdown", "content": (
                f"类型：{str(memory.get('memory_type') or '')}\n\n"
                f"内容：{str(memory.get('content') or '')[:500]}\n\n"
                "修改保存成功后，旧记忆会被替代。"
            )},
            {"tag": "action", "actions": [
                {"tag": "button", "text": {"tag": "plain_text", "content": "修改"}, "type": "primary", "value": {"mindflow_action": "memory_edit_open", "version": "1", "memory_id": str(memory.get("id") or "")}},
                {"tag": "button", "text": {"tag": "plain_text", "content": "删除"}, "type": "danger", "value": {"mindflow_action": "memory_delete_prompt", "version": "1", "memory_id": str(memory.get("id") or "")}},
                {"tag": "button", "text": {"tag": "plain_text", "content": "返回"}, "value": {"mindflow_action": "memory_center_refresh", "version": "1"}},
            ]},
        ],
    }


def memory_edit_card(memory: dict[str, Any]) -> dict[str, Any]:
    return {
        "header": {"template": "blue", "title": {"tag": "plain_text", "content": "修改记忆"}},
        "elements": [{
            "tag": "form", "name": "memory_edit",
            "elements": [
                {
                    "tag": "input", "name": "content",
                    "label": {"tag": "plain_text", "content": "记忆内容"},
                    "default_value": str(memory.get("content") or "")[:500],
                    "placeholder": {"tag": "plain_text", "content": "输入新的记忆内容"},
                },
                {
                    "tag": "button", "name": "memory_edit_submit",
                    "text": {"tag": "plain_text", "content": "保存修改"},
                    "type": "primary", "action_type": "form_submit",
                    "value": {"mindflow_action": "memory_edit_save", "version": "1", "memory_id": str(memory.get("id") or "")},
                },
            ],
        }, {
            "tag": "action", "actions": [{
                "tag": "button", "text": {"tag": "plain_text", "content": "取消"},
                "value": {"mindflow_action": "memory_detail_open", "version": "1", "memory_id": str(memory.get("id") or "")},
            }],
        }],
    }


def memory_delete_confirmation_card(memory: dict[str, Any]) -> dict[str, Any]:
    return {
        "header": {"template": "orange", "title": {"tag": "plain_text", "content": "确认删除记忆"}},
        "elements": [
            {"tag": "markdown", "content": f"将删除：\n\n{str(memory.get('content') or '')[:500]}"},
            {"tag": "action", "actions": [
                {"tag": "button", "text": {"tag": "plain_text", "content": "确认删除"}, "type": "danger", "value": {"mindflow_action": "memory_delete_confirm", "version": "1", "memory_id": str(memory.get("id") or "")}},
                {"tag": "button", "text": {"tag": "plain_text", "content": "取消"}, "value": {"mindflow_action": "memory_center_refresh", "version": "1"}},
            ]},
        ],
    }


def memory_clear_all_confirmation_card() -> dict[str, Any]:
    return {
        "header": {"template": "red", "title": {"tag": "plain_text", "content": "确认清空全部记忆"}},
        "elements": [
            {"tag": "markdown", "content": "这只会清空你的长期个性化记忆，不会删除日历、状态记录、研究数据、预测或授权。"},
            {"tag": "action", "actions": [
                {"tag": "button", "text": {"tag": "plain_text", "content": "确认清空"}, "type": "danger", "value": {"mindflow_action": "memory_clear_confirm", "version": "1"}},
                {"tag": "button", "text": {"tag": "plain_text", "content": "取消"}, "value": {"mindflow_action": "memory_center_refresh", "version": "1"}},
            ]},
        ],
    }


def personalization_proposal_confirmation_card(
    proposal: dict[str, Any],
) -> dict[str, Any]:
    """Fixed review card for typed memory and preference proposals."""

    proposal_id = str(proposal.get("id") or "").strip()
    domain = str(proposal.get("domain") or "").strip()
    operation = str(proposal.get("operation") or "").strip()
    payload = dict(proposal.get("payload") or {})
    if not proposal_id or len(proposal_id) > 64:
        raise ValueError("personalization proposal id is invalid")
    lines: list[str]
    destructive = False
    if domain == "memory" and operation == "remember":
        title = "确认保存长期记忆"
        lines = [
            f"**内容：** {str(payload.get('content') or '')[:500]}",
            f"**类型：** {str(payload.get('memory_type') or '')[:40]}",
            "确认前不会写入长期记忆。",
        ]
    elif domain == "memory" and operation == "replace":
        title = "确认修改长期记忆"
        lines = [
            f"**原内容：** {str(payload.get('previous_content') or '')[:500]}",
            f"**新内容：** {str(payload.get('content') or '')[:500]}",
        ]
    elif domain == "memory" and operation == "delete":
        title = "确认删除长期记忆"
        destructive = True
        lines = [f"**将删除：** {str(payload.get('previous_content') or '')[:500]}"]
    elif domain == "memory" and operation == "clear":
        title = "确认清空长期记忆"
        destructive = True
        items = [dict(item) for item in list(payload.get("items") or [])]
        lines = [f"将删除当前审核列表中的 **{len(items)}** 条长期记忆："]
        lines.extend(f"- {str(item.get('content') or '')[:120]}" for item in items[:10])
        if len(items) > 10:
            lines.append(f"- 以及其余 {len(items) - 10} 条")
    elif domain in {"interaction_preferences", "support_preferences"} and operation == "update":
        title = "确认修改表达与支持偏好"
        labels = {
            "verbosity": "回答长度",
            "tone": "语气",
            "suggestion_style": "建议方式",
            "assistant_display_name": "助手显示名",
            "assistant_self_reference": "助手自称",
            "acknowledge_before_advice": "建议前先回应感受",
            "ask_before_suggestion": "建议前先询问",
            "max_suggestions": "每次最多建议数",
            "allow_supportive_follow_up": "允许支持性跟进",
            "preferred_support_style": "支持方式",
        }
        changes = {}
        for group in ("style_changes", "identity_changes", "support_changes"):
            changes.update(dict(payload.get(group) or {}))
        lines = [
            f"- **{labels.get(key, key)}：** {value}"
            for key, value in changes.items()
        ]
        lines.append("这些设置不能改变安全、授权或权限规则。")
    else:
        raise ValueError("personalization proposal is invalid")
    return {
        "schema": "2.0",
        "config": {
            "update_multi": True,
            "width_mode": "fill",
            "enable_forward": False,
            "summary": {"content": title},
        },
        "header": {
            "template": "red" if destructive else "blue",
            "title": {"tag": "plain_text", "content": title},
        },
        "body": {
            "direction": "vertical",
            "elements": [
                {"tag": "markdown", "content": "\n\n".join(lines)},
                {
                    "tag": "button",
                    "type": "danger" if destructive else "primary",
                    "text": {"tag": "plain_text", "content": "确认"},
                    "behaviors": [{
                        "type": "callback",
                        "value": {
                            "mindflow_action": "personalization_proposal_confirm",
                            "version": "1",
                            "proposal_id": proposal_id,
                        },
                    }],
                },
                {
                    "tag": "button",
                    "type": "default",
                    "text": {"tag": "plain_text", "content": "取消"},
                    "behaviors": [{
                        "type": "callback",
                        "value": {
                            "mindflow_action": "personalization_proposal_cancel",
                            "version": "1",
                            "proposal_id": proposal_id,
                        },
                    }],
                },
            ],
        },
    }


def preference_settings_card(preferences: dict[str, Any]) -> dict[str, Any]:
    support = dict(preferences.get("support") or {})

    def options(values: tuple[str, ...]) -> list[dict[str, Any]]:
        return [{"text": {"tag": "plain_text", "content": value}, "value": value} for value in values]

    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": "blue", "title": {"tag": "plain_text", "content": "表达与支持偏好"}},
        "elements": [
            {"tag": "markdown", "content": "这些设置只影响表达和建议方式，不能改变安全规则、授权或工具权限。"},
            {"tag": "form", "name": "interaction_preference_settings", "elements": [
                {"tag": "select_static", "name": "verbosity", "placeholder": {"tag": "plain_text", "content": "回答长度"}, "initial_option": str(preferences.get("verbosity") or "balanced"), "options": options(("concise", "balanced", "detailed"))},
                {"tag": "select_static", "name": "tone", "placeholder": {"tag": "plain_text", "content": "语气"}, "initial_option": str(preferences.get("tone") or "warm"), "options": options(("neutral", "warm", "direct"))},
                {"tag": "select_static", "name": "suggestion_style", "placeholder": {"tag": "plain_text", "content": "建议方式"}, "initial_option": str(preferences.get("suggestion_style") or "light_suggestions"), "options": options(("ask_first", "light_suggestions", "proactive_suggestions"))},
                {"tag": "select_static", "name": "max_suggestions", "placeholder": {"tag": "plain_text", "content": "每次最多建议数"}, "initial_option": str(support.get("max_suggestions") or 3), "options": options(("1", "2", "3"))},
                {"tag": "button", "name": "interaction_preference_settings_submit", "text": {"tag": "plain_text", "content": "保存"}, "type": "primary", "action_type": "form_submit", "value": {"mindflow_action": "preference_settings_save", "version": "1"}},
            ]},
            {"tag": "action", "actions": [
                {"tag": "button", "text": {"tag": "plain_text", "content": "先听我说，再建议"}, "value": {"mindflow_action": "support_acknowledge_first", "version": "1"}},
                {"tag": "button", "text": {"tag": "plain_text", "content": "关闭支持性跟进"}, "value": {"mindflow_action": "support_followup_disable", "version": "1"}},
            ]},
        ],
    }
