"""Stable user-facing capability copy for deterministic infrastructure routes."""

from __future__ import annotations


def onboarding_text(participant_code: str) -> str:
    code = str(participant_code).strip()
    return (
        f"绑定好了（{code}）。\n\n"
        "你可以直接告诉我：\n"
        "• 看今天或明天的压力曲线\n"
        "• 记录当前状态，或让我发一张状态填写卡\n"
        "• 查看、添加或修改单次/周期日程；第一次使用日历请发 /calendar\n"
        "• 发课程表图片，我会先让你确认时间、周次和重复方式，再添加到日历\n"
        "• 查看近期状态，或调整提醒和静默时间\n\n"
        "需要时发“功能”就能再看一次。"
    )


def help_text() -> str:
    return (
        "目前可以用这些：\n\n"
        "• 压力曲线：看今天、明天或已有的历史预测\n"
        "• 状态记录：记录当前压力和精力，也可以用填写卡\n"
        "• 日历：查看、添加或修改单次/周期日程\n"
        "• 课程表导入：识别后会先让你确认时间、周次和重复方式，再写入日历\n"
        "• 每日回顾：记录一天开始、峰值和结束时的状态\n"
        "• 提醒设置：可以调整提醒、静默时间和关怀偏好\n\n"
        "日历第一次使用时，先发 /calendar 完成授权。"
    )
