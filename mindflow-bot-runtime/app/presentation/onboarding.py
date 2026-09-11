"""Reviewed onboarding copy for unbound and freshly bound participants.

Binding copy never leads with a command, never says "错误", and never asks the
participant to remember syntax; /bind stays available as a compatibility
mention only. Bind failures are deliberately worded without distinguishing
invalid from expired or used codes, because that distinction would disclose
whether a token exists.
"""

from __future__ import annotations


def unbound_welcome_text() -> str:
    return (
        "你好，我是 MindFlow 🌱\n\n"
        "这是一份只属于你的日常小助手，需要用研究者发给你的绑定码开启。\n\n"
        "直接把绑定码发给我就可以开始；发“/bind 绑定码”也可以。"
    )


def invalid_invite_text() -> str:
    return (
        "这个绑定码我这里没有对上。它可能还没生成、已经过期，或者已经被使用了。\n\n"
        "确认后再发一次就可以；如果需要新的绑定码，找研究者重新生成一份即可。"
    )


def bind_unavailable_text() -> str:
    return (
        "绑定服务这会儿暂时不可用，请稍等片刻再把绑定码发给我一次。"
    )


def already_bound_text() -> str:
    return "这个飞书账号已经绑定过了，直接发消息就可以继续使用。"


def welcome_first_screen_text() -> str:
    return (
        "绑定好了，欢迎使用 MindFlow 🌱\n\n"
        "不用一次了解全部，可以先从一件事开始：\n\n"
        "• 想记录状态：直接说“记一下，我现在压力 6、精力 5”，或者发“给我个表填状态”\n"
        "• 想看安排：发“看看今天的安排”或“今天的压力曲线”\n"
        "• 想导入课程表：直接发课程表图片，我会先让你确认再添加\n"
        "• 想看全部能力：发“功能”就可以\n\n"
        "你也可以就像聊天一样，先说说你现在怎么样。"
    )
