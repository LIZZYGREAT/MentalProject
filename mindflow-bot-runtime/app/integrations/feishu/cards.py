"""Reviewed Feishu card builders for participant-visible rich replies."""

from __future__ import annotations

from typing import Any

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
        "config": {
            "wide_screen_mode": True,
            "update_multi": False,
            "enable_forward": False,
        },
        "header": {
            "template": "turquoise",
            "title": {"tag": "plain_text", "content": "每日状态记录"},
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "请按此刻感受填写。0 表示最低，10 表示最高。",
                },
            },
            {
                "tag": "form",
                "name": "mindflow_daily_checkin",
                "elements": [
                    {
                        "tag": "select_static",
                        "name": "stress",
                        "required": True,
                        "placeholder": {"tag": "plain_text", "content": "选择 0–10"},
                        "label": {"tag": "plain_text", "content": "当前压力"},
                        "options": scale_options,
                    },
                    {
                        "tag": "select_static",
                        "name": "energy",
                        "required": True,
                        "placeholder": {"tag": "plain_text", "content": "选择 0–10"},
                        "label": {"tag": "plain_text", "content": "当前精力"},
                        "options": scale_options,
                    },
                    {
                        "tag": "input",
                        "name": "activity",
                        "required": True,
                        "max_length": 120,
                        "placeholder": {"tag": "plain_text", "content": "例如：在图书馆写作业"},
                        "label": {"tag": "plain_text", "content": "正在做什么"},
                    },
                    {
                        "tag": "select_static",
                        "name": "stress_event_since_last",
                        "required": True,
                        "placeholder": {"tag": "plain_text", "content": "请选择"},
                        "label": {"tag": "plain_text", "content": "上次记录后有压力事件吗"},
                        "options": boolean_options,
                    },
                    {
                        "tag": "select_static",
                        "name": "event_ongoing",
                        "required": True,
                        "placeholder": {"tag": "plain_text", "content": "请选择"},
                        "label": {"tag": "plain_text", "content": "该事件仍在持续吗"},
                        "options": boolean_options,
                    },
                    {
                        "tag": "button",
                        "name": "submit_checkin",
                        "action_type": "form_submit",
                        "type": "primary",
                        "text": {"tag": "plain_text", "content": "提交记录"},
                        "value": {"mindflow_action": "submit_checkin", "version": "1"},
                        "confirm": {
                            "title": {"tag": "plain_text", "content": "提交状态记录"},
                            "text": {"tag": "plain_text", "content": "确认提交本次记录吗？"},
                        },
                    },
                ],
            },
            {
                "tag": "note",
                "elements": [
                    {
                        "tag": "plain_text",
                        "content": "用于日常状态建模，不是医学问卷或诊断量表。",
                    }
                ],
            },
        ],
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
    fields = [
        ("start_stress", "早晨起始压力"), ("start_energy", "早晨起始精力"),
        ("peak_stress", "全天最高压力"), ("end_stress", "当前/收尾压力"),
        ("end_energy", "当前/收尾精力"), ("energy_consumption", "全天精力消耗"),
    ]
    elements: list[dict[str, Any]] = []
    for name, label in fields:
        elements.append({
            "tag": "select_static", "name": name, "required": True,
            "placeholder": {"tag": "plain_text", "content": "选择 0–10"},
            "label": {"tag": "plain_text", "content": label},
            "options": scale_options,
        })
    elements.insert(3, {
        "tag": "select_static", "name": "peak_period", "required": True,
        "placeholder": {"tag": "plain_text", "content": "选择大致时段"},
        "label": {"tag": "plain_text", "content": "最高压力出现时段"},
        "options": [
            {"text": {"tag": "plain_text", "content": label}, "value": value}
            for label, value in period_options
        ],
    })
    for name, label, maximum in (
        ("main_stressor", "主要压力来源（选填）", 300),
        ("recovery_note", "有效恢复方式（选填）", 300),
        ("free_text", "其他补充（选填）", 1000),
    ):
        elements.append({
            "tag": "input", "name": name, "required": False, "max_length": maximum,
            "placeholder": {"tag": "plain_text", "content": "可留空"},
            "label": {"tag": "plain_text", "content": label},
        })
    elements.append({
        "tag": "button", "name": "daily_review_submit",
        "action_type": "form_submit", "type": "primary",
        "text": {"tag": "plain_text", "content": "提交每日回顾"},
        "value": {
            "mindflow_action": "daily_review_submit", "version": "1",
            "schedule_id": schedule_id, "local_date": local_date,
            "card_version": card_version,
        },
    })
    return {
        "config": {"wide_screen_mode": True, "update_multi": False, "enable_forward": False},
        "header": {"template": "purple", "title": {"tag": "plain_text", "content": "MindFlow 每日回顾"}},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": f"回顾 **{local_date}**。这是回顾反馈，不会改写当天原始预测。"}},
            {"tag": "form", "name": "mindflow_daily_review", "elements": elements},
            {"tag": "note", "elements": [{"tag": "plain_text", "content": "0 表示最低，10 表示最高；用于日常建模，不是医学诊断。"}]},
        ],
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
                    "value": {"mindflow_action": "request_checkin", "version": "1"},
                },
                {
                    "tag": "button",
                    "type": "default",
                    "text": {"tag": "plain_text", "content": "查看今日日程"},
                    "value": {"mindflow_action": "view_today_calendar", "version": "1"},
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
                    "value": {"mindflow_action": "request_checkin", "version": "1"},
                },
            ]
        },
    }
