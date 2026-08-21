"""Reviewed Feishu card builders for participant-visible rich replies."""

from __future__ import annotations

from typing import Any


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
    curve: list[dict[str, Any]], *, local_date: str
) -> dict[str, Any]:
    """Build a Feishu Card 2.0 chart without exposing arbitrary card JSON."""

    values: list[dict[str, Any]] = []
    for point in curve[:96]:
        time_label = str(point.get("time") or "")[:16]
        if not time_label:
            continue
        try:
            stress = max(0.0, min(10.0, float(point.get("stress_0_10"))))
        except (TypeError, ValueError):
            continue
        values.append({"time": time_label, "value": round(stress, 2), "metric": "压力"})
        try:
            vitality = max(0.0, min(10.0, float(point.get("vitality_0_10"))))
        except (TypeError, ValueError):
            continue
        values.append({"time": time_label, "value": round(vitality, 2), "metric": "活力"})

    if not values:
        raise ValueError("pressure curve has no plottable points")

    stress_values = [item["value"] for item in values if item["metric"] == "压力"]
    peak = max(stress_values)
    peak_time = next(
        item["time"]
        for item in values
        if item["metric"] == "压力" and item["value"] == peak
    )
    summary = f"预测压力峰值约 **{peak:g}/10**，时间 **{peak_time}**。"
    if not any(item["metric"] == "活力" for item in values):
        summary += " 本次预测未包含活力曲线。"

    return {
        "schema": "2.0",
        "config": {
            "width_mode": "fill",
            "enable_forward": False,
            "summary": {"content": "今日压力与活力曲线"},
        },
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": "今日压力与活力曲线"},
            "subtitle": {"tag": "plain_text", "content": str(local_date)[:10]},
        },
        "body": {
            "direction": "vertical",
            "elements": [
                {
                    "tag": "chart",
                    "aspect_ratio": "16:9",
                    "height": "320px",
                    "preview": True,
                    "color_theme": "brand",
                    "chart_spec": {
                        "type": "line",
                        "data": [{"id": "curve", "values": values}],
                        "xField": "time",
                        "yField": "value",
                        "seriesField": "metric",
                        "point": {"visible": True},
                        "axes": [
                            {"orient": "bottom", "type": "band"},
                            {
                                "orient": "left",
                                "type": "linear",
                                "min": 0,
                                "max": 10,
                                "tick": {"tickCount": 6},
                            },
                        ],
                        "legends": {"visible": True, "orient": "bottom"},
                    },
                },
                {"tag": "markdown", "content": summary},
                {
                    "tag": "markdown",
                    "text_size": "notation",
                    "content": "这是模型预测，不是已观察事实或医疗判断。",
                },
            ],
        },
    }
