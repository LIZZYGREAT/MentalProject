"""Reviewed consent copy for surfaces that cannot render interactive cards."""

from __future__ import annotations


def external_llm_consent_prompt_text() -> str:
    """Fallback when the consent card cannot be delivered.

    Points at the backend-only navigation path (功能 → 数据与隐私), so consent
    never depends on the agent being available first.
    """

    return (
        "外部 AI 处理需要你的同意：\n\n"
        "为了回复你的消息、识别你主动发送的图片，相关内容会发送给外部 AI 模型处理；"
        "MindFlow 不会把你的账号标识作为提示内容发送给模型。\n\n"
        "发“功能”打开功能一览，在“数据与隐私”里选择“外部 AI 处理”即可开启。"
    )


def external_llm_consent_declined_text() -> str:
    return (
        "好的，这次先不开启。之后想开启的话，"
        "发“功能”打开功能一览，在“数据与隐私”里选择“外部 AI 处理”就可以。"
    )
