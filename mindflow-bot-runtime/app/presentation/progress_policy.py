"""Narrow policy for suppressing generic progress on short social turns."""

from __future__ import annotations


_SILENT_SOCIAL_MESSAGES = frozenset(
    {
        "你好",
        "您好",
        "嗨",
        "哈喽",
        "在吗",
        "在不在",
        "谢谢",
        "谢了",
        "好的",
        "好",
        "ok",
        "嗯",
        "嗯嗯",
        "收到",
        "明白了",
        "知道了",
        "晚安",
        "早",
        "早上好",
        "下午好",
        "晚上好",
    }
)
_TRAILING_PUNCTUATION = "?？!！.。"


def should_force_silent_progress(user_text: str) -> bool:
    """Return true only for an explicit, short social acknowledgement."""

    normalized = str(user_text or "").strip().casefold()
    if not normalized or len(normalized) > 12:
        return False
    normalized = normalized.rstrip(_TRAILING_PUNCTUATION).strip()
    return bool(normalized and normalized in _SILENT_SOCIAL_MESSAGES)
