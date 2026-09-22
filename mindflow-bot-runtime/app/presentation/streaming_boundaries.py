"""Safe cumulative-prefix boundaries for validated Markdown streaming."""

from __future__ import annotations

import math
import re


_MARKDOWN_LINK = re.compile(r"\[[^\]\n]+\]\([^\s)]+(?:\s+['\"][^)]*['\"])?\)")
_RAW_URL = re.compile(r"https?://[^\s<>()\[\]\"']+", re.IGNORECASE)
_BOLD = re.compile(r"\*\*(?:[^*]|\*(?!\*))*\*\*", re.DOTALL)
_INLINE_CODE = re.compile(r"`[^`\n]*`")
_FENCE = re.compile(r"```[^\n]*\n.*?```", re.DOTALL)


def safe_stream_prefix_boundaries(
    text: str,
    *,
    max_updates: int = 60,
    min_chars: int = 1,
) -> tuple[int, ...]:
    """Return at most ``max_updates`` safe cumulative boundaries, including end.

    A boundary is never placed inside the small Markdown structures that Feishu
    would render inconsistently while a card is being updated.
    """

    value = str(text or "")
    length = len(value)
    if not value:
        return ()
    maximum = max(1, int(max_updates))
    minimum = max(1, int(min_chars))
    protected = _protected_spans(value)
    candidates = [
        position
        for position in _candidate_positions(value)
        if not _inside_protected(position, protected)
    ]
    if length not in candidates:
        candidates.append(length)
    candidates = sorted(set(candidates))
    target = max(minimum, math.ceil(length / maximum))
    boundaries: list[int] = []
    cursor = 0
    while cursor < length and len(boundaries) < maximum:
        goal = min(length, cursor + target)
        before_goal = [
            point
            for point in candidates
            if cursor + minimum <= point <= goal
        ]
        if before_goal:
            selected = before_goal[-1]
        else:
            selected = next(
                (point for point in candidates if point >= goal),
                length,
            )
        if selected <= cursor:
            selected = length
        boundaries.append(selected)
        cursor = selected
    if boundaries[-1] != length:
        boundaries[-1] = length
    return tuple(boundaries)


def _candidate_positions(value: str) -> tuple[int, ...]:
    positions: set[int] = set()
    for index, char in enumerate(value, start=1):
        if char in "。！？!?\n" or char.isspace():
            positions.add(index)
    return tuple(sorted(positions))


def _protected_spans(value: str) -> tuple[tuple[int, int], ...]:
    spans = [
        *(match.span() for match in _MARKDOWN_LINK.finditer(value)),
        *(match.span() for match in _RAW_URL.finditer(value)),
        *(match.span() for match in _BOLD.finditer(value)),
        *(match.span() for match in _INLINE_CODE.finditer(value)),
        *(match.span() for match in _FENCE.finditer(value)),
    ]
    return tuple(sorted(spans))


def _inside_protected(position: int, spans: tuple[tuple[int, int], ...]) -> bool:
    return any(start < position < end for start, end in spans)
