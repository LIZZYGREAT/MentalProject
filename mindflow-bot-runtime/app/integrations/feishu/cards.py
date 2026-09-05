"""Reviewed Feishu card builders for participant-visible rich replies."""

from __future__ import annotations

from typing import Any
from zoneinfo import ZoneInfo

from app.domain.course_schedule_recurrence import (
    EXPAND_ALL_OCCURRENCES,
    PRESERVE_SCHEDULE_PATTERN,
    describe_course_write_plan,
    plan_course_writes,
)


def course_schedule_preview_card(draft: dict[str, Any]) -> dict[str, Any]:
    """Fixed schedule preview; callback carries only the opaque draft id."""

    structured = dict(draft.get("structured_result") or {})
    courses = list(structured.get("courses") or [])
    items = list(draft.get("items") or [])
    missing = set(structured.get("missing_context") or [])
    lines = [f"识别到 **{len(courses)}** 门课"]
    uncertain: list[str] = []
    weekday_names = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
    for index, course in enumerate(courses[:20]):
        name = _safe_schedule_text(course.get("course_name") or "未命名课程")
        weekday = course.get("weekday")
        day = weekday_names[int(weekday) - 1] if isinstance(weekday, int) and 1 <= weekday <= 7 else "星期待确认"
        start = course.get("start_time")
        end = course.get("end_time")
        period = (
            f"{start}–{end}" if start and end
            else f"第{course.get('period_start')}–{course.get('period_end')}节"
            if course.get("period_start") and course.get("period_end")
            else "时间待确认"
        )
        rule = dict(course.get("week_rule") or {})
        if rule.get("explicit_weeks"):
            week_text = ",".join(str(value) for value in rule["explicit_weeks"]) + "周"
        else:
            week_text = f"{rule.get('start_week')}–{rule.get('end_week')}周"
            if rule.get("odd_even") == "odd":
                week_text += "单周"
            elif rule.get("odd_even") == "even":
                week_text += "双周"
        location = _safe_schedule_text(course.get("location") or "地点待确认")
        lines.extend(["", f"**{name}**", f"{day} {period} · {week_text} · {location}"])
        if not missing and index < len(items):
            writes = plan_course_writes(
                draft,
                items[index],
                strategy=PRESERVE_SCHEDULE_PATTERN,
                timezone=ZoneInfo(str(draft.get("timezone") or "Asia/Shanghai")),
            )
            lines.append(f"重复方式：{describe_course_write_plan(writes)}")
        fields = list(course.get("uncertain_fields") or [])
        if fields:
            uncertain.append(f"- {name}：{', '.join(_safe_schedule_text(v) for v in fields)}")
    if len(courses) > 20:
        lines.extend(["", "课程数量超过 20 项，请拆分图片后重新导入。"])
    warnings = [_safe_schedule_text(value) for value in structured.get("warnings") or []]
    if uncertain or warnings:
        lines.extend(["", f"有 {len(uncertain) + len(warnings)} 项需要你确认", *uncertain, *[f"- {v}" for v in warnings]])
    if "semester_start_date" in missing:
        lines.extend(["", "这张课表里没有学期第一周日期。", "告诉我第一周周一是哪天就可以继续。"])
    if missing & {"period_time_mapping", "actual_time"}:
        lines.extend(["", "这张课表只有“第1-2节”这类节次，没有具体上课时间。", "把学校作息时间告诉我后，我再生成日历。"])
    elements: list[dict[str, Any]] = [{"tag": "markdown", "content": "\n".join(lines)}]
    status = str(draft.get("status") or "")
    if not missing and len(courses) <= 20 and status == "pending_confirmation":
        elements.extend([
            _schedule_action_button(
                draft["id"],
                "按课表周期规则添加",
                strategy=PRESERVE_SCHEDULE_PATTERN,
                primary=True,
            ),
            _schedule_action_button(
                draft["id"],
                "全部拆成单次日程",
                strategy=EXPAND_ALL_OCCURRENCES,
            ),
            _schedule_cancel_button(draft["id"]),
        ])
        lines.append("\n确认无误后，我再添加到日历。")
        elements[0]["content"] = "\n".join(lines)
    elif not missing and status == "partial_failed" and draft.get("recurrence_strategy"):
        elements.append(_schedule_action_button(
            draft["id"],
            "重试失败项",
            strategy=str(draft["recurrence_strategy"]),
            primary=True,
        ))
    elif status == "running":
        lines.append("\n正在添加到日历，请稍候。")
        elements[0]["content"] = "\n".join(lines)
    elif status == "succeeded":
        lines.append("\n这份课程表已经添加到日历。")
        elements[0]["content"] = "\n".join(lines)
    elif status in {"cancelled", "expired"}:
        lines.append("\n这份课程表导入已取消或过期，请重新发送图片。")
        elements[0]["content"] = "\n".join(lines)
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
    if error == "calendar_not_connected" and import_id and recurrence_strategy:
        elements.append(_schedule_action_button(
            import_id,
            "重试失败项" if status == "partial_failed" else "继续按已选择策略添加",
            strategy=recurrence_strategy,
            primary=True,
        ))
        if status != "partial_failed":
            elements.append(_schedule_cancel_button(import_id))
    elif status == "partial_failed" and import_id and recurrence_strategy:
        elements.append(_schedule_action_button(
            import_id,
            "重试失败项",
            strategy=recurrence_strategy,
            primary=True,
        ))
    elif (
        error == "calendar_write_limit_exceeded"
        and import_id
        and recurrence_strategy == EXPAND_ALL_OCCURRENCES
    ):
        elements.extend([
            _schedule_action_button(
                import_id,
                "改用按课表周期规则添加",
                strategy=PRESERVE_SCHEDULE_PATTERN,
                primary=True,
            ),
            _schedule_cancel_button(import_id),
        ])
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


def _schedule_cancel_button(import_id: str) -> dict[str, Any]:
    return {
        "tag": "button",
        "type": "default",
        "text": {"tag": "plain_text", "content": "取消"},
        "behaviors": [{"type": "callback", "value": {
            "mindflow_action": "course_schedule_import_cancel",
            "version": "2",
            "import_id": str(import_id),
        }}],
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
                    "text": {"tag": "plain_text", "content": "查看今日日程"},
                    "behaviors": [{
                        "type": "callback",
                        "value": {
                            "mindflow_action": "view_today_calendar",
                            "version": "1",
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
    events: list[dict[str, Any]], *, local_date: str
) -> dict[str, Any]:
    lines = []
    for event in events[:20]:
        start = str(event.get("start_time") or "")
        label = start.split("T", 1)[-1][:5] if start else "全天"
        summary = str(event.get("summary") or "未命名日程")[:80]
        lines.append(f"• **{label}** {summary}")
    content = "\n".join(lines) if lines else "今天暂无日程。"
    return {
        "schema": "2.0",
        "config": {"width_mode": "fill", "enable_forward": False},
        "header": {
            "template": "turquoise",
            "title": {"tag": "plain_text", "content": "今日日程"},
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
