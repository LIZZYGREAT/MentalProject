"""Deterministic Feishu cards that never expose internal credentials."""

from __future__ import annotations

from typing import Any, Dict


def _plain(text: str) -> Dict[str, str]:
    return {"tag": "plain_text", "content": str(text)}


def binding_card(bind_url: str, expires_at: str) -> Dict[str, Any]:
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": "blue", "title": _plain("绑定关怀服务账号")},
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "绑定后，机器人才能安全读取你自己的状态与预测；你可以随时在 Web 设置中解绑。",
                },
            },
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "type": "primary",
                        "text": _plain("登录并确认绑定"),
                        "url": str(bind_url),
                    }
                ],
            },
            {
                "tag": "note",
                "elements": [_plain(f"链接为一次性凭证，有效期至 {expires_at}")],
            },
        ],
    }


def checkin_card() -> Dict[str, Any]:
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": "turquoise", "title": _plain("记录此刻")},
        "elements": [
            {
                "tag": "form",
                "name": "care_checkin",
                "elements": [
                    {
                        "tag": "input",
                        "name": "stress_0_10",
                        "label": _plain("压力（0–10）"),
                        "placeholder": _plain("例如 7"),
                    },
                    {
                        "tag": "input",
                        "name": "vitality_0_10",
                        "label": _plain("活力（0–10）"),
                        "placeholder": _plain("例如 4"),
                    },
                    {
                        "tag": "input",
                        "name": "activity",
                        "label": _plain("当前活动"),
                        "placeholder": _plain("例如 上课、写作业、休息"),
                    },
                    {
                        "tag": "select_static",
                        "name": "stress_event_since_last",
                        "label": _plain("自上次以来发生过压力事件吗？"),
                        "options": [
                            {"text": _plain("是"), "value": "true"},
                            {"text": _plain("否"), "value": "false"},
                        ],
                    },
                    {
                        "tag": "select_static",
                        "name": "event_ongoing",
                        "label": _plain("事件仍在持续吗？"),
                        "options": [
                            {"text": _plain("是"), "value": "true"},
                            {"text": _plain("否"), "value": "false"},
                        ],
                    },
                    {
                        "tag": "button",
                        "type": "primary",
                        "name": "care_checkin_submit",
                        "text": _plain("提交打卡"),
                        "action_type": "form_submit",
                        "value": {"action": "care_checkin_submit"},
                    },
                ],
            }
        ],
    }


def feedback_card(text: str, delivery_id: str) -> Dict[str, Any]:
    actions = []
    for label, value in (
        ("有帮助", "helpful"),
        ("没太大帮助", "not_helpful"),
        ("稍后提醒", "remind_later"),
        ("今天不再提醒", "mute_today"),
    ):
        actions.append(
            {
                "tag": "button",
                "text": _plain(label),
                "type": "primary" if value == "helpful" else "default",
                "value": {
                    "action": "care_feedback",
                    "review": value,
                    "delivery_id": str(delivery_id),
                },
            }
        )
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": "green", "title": _plain("给自己一点支持")},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": str(text)}},
            {"tag": "action", "actions": actions},
        ],
    }


def calendar_connection_card(connect_url: str) -> Dict[str, Any]:
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": "blue", "title": _plain("连接你自己的飞书日历")},
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "仅在你授权后读取个人日历，用于负荷预测和关怀建议；机器人不会在卡片中保存或展示凭证。",
                },
            },
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "type": "primary",
                        "text": _plain("登录 Web 并连接日历"),
                        "url": str(connect_url),
                    }
                ],
            },
        ],
    }


def help_card() -> Dict[str, Any]:
    actions = []
    for label, action in (
        ("记录此刻", "care_open_checkin"),
        ("查看今天状态", "care_get_today"),
        ("运行今日评估", "care_run_assessment"),
        ("给我一点支持", "care_get_support"),
    ):
        actions.append(
            {
                "tag": "button",
                "text": _plain(label),
                "value": {"action": action},
            }
        )
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": "turquoise", "title": _plain("关怀助手")},
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "我可以帮你记录此刻、查看当天状态、运行已有模型评估并提供非临床的支持建议。",
                },
            },
            {"tag": "action", "actions": actions},
        ],
    }
