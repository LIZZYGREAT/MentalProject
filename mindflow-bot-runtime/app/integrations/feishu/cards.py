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


def pressure_curve_card(
    analysis: CurveAnalysis,
    *,
    image_key: str,
    local_date: str,
    model_output: dict[str, Any] | None = None,
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
            f"**今日峰值：** {analysis.peak_stress:g}/10（{analysis.peak_stress_time}）"
        )
        title = f"今日 {model.variant.upper()} 压力与活力曲线"
    else:
        summary = (
            f"**当前压力：** {analysis.current_stress:g}/10\n"
            f"**今日峰值：** {analysis.peak_stress:g}/10（{analysis.peak_stress_time}）\n"
            f"**模型：** {model.family}（仅压力状态 S）"
        )
        title = "今日 M0 压力预测曲线"

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
                {"tag": "markdown", "content": "**关键节点**\n" + node_text},
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
                    "content": "这是模型预测，不是已观察事实或医疗判断。",
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
