"""Sentence-aware segmentation for a small number of Feishu messages."""

from __future__ import annotations

import re


_STRONG_BOUNDARY = re.compile(r"(?<=[。！？!?；;])(?:\s+|(?=[^\s]))|\n{2,}")
_URL = re.compile(r"https?://\S+")
_MARKDOWN_LINK = re.compile(r"!?\[[^\]]*\]\([^\n)]*\)")
_WEAK_CHARS = "，、,：: \n"


class SemanticSegmenter:
    def __init__(
        self,
        *,
        enabled: bool = True,
        min_total_chars: int = 320,
        target_chars: int = 260,
        max_chars: int = 650,
        max_segments: int = 3,
    ):
        self.enabled = bool(enabled)
        self.min_total_chars = max(1, int(min_total_chars))
        self.target_chars = max(1, int(target_chars))
        self.max_chars = max(self.target_chars, int(max_chars))
        self.max_segments = max(1, int(max_segments))

    def segment(self, text: str) -> tuple[str, ...]:
        value = str(text or "").strip()
        if not value:
            return ()
        if not self.enabled or len(value) < self.min_total_chars:
            return (value,)
        # The delivery envelope is a UX target, not permission to discard an
        # authoritative business result. PresentationAgent may safely compress
        # validated content, but deterministic fallback must remain lossless.
        if len(value) > self.max_segments * self.max_chars:
            return (value,)

        units: list[str] = []
        for unit in _STRONG_BOUNDARY.split(value):
            unit = unit.strip()
            if not unit:
                continue
            units.extend(self._split_oversized(unit))

        packed: list[str] = []
        current = ""
        for unit in units:
            separator = "\n\n" if "\n" in unit or "\n" in current else ""
            candidate = f"{current}{separator}{unit}" if current else unit
            if current and (
                len(candidate) > self.max_chars
                or (len(current) >= self.target_chars and len(packed) < self.max_segments - 1)
            ):
                packed.append(current.strip())
                current = unit
            else:
                current = candidate
        if current.strip():
            packed.append(current.strip())

        while len(packed) > self.max_segments:
            best_index = self._best_merge_index(packed)
            if best_index is None:
                return self._capacity_partition(value)
            packed[best_index : best_index + 2] = [
                f"{packed[best_index]}\n\n{packed[best_index + 1]}".strip()
            ]
        if any(len(item) > self.max_chars for item in packed):
            return self._capacity_partition(value)
        return tuple(item for item in packed if item)

    def _split_oversized(self, value: str) -> list[str]:
        parts: list[str] = []
        remaining = value.strip()
        while len(remaining) > self.max_chars:
            cut = self._safe_cut(remaining, self.max_chars)
            parts.append(remaining[:cut].strip())
            remaining = remaining[cut:].strip()
        if remaining:
            parts.append(remaining)
        return parts

    def _safe_cut(self, value: str, limit: int) -> int:
        floor = max(1, min(limit - 1, self.target_chars // 2))
        protected = [(m.start(), m.end()) for m in _URL.finditer(value)]
        for index in range(min(limit, len(value) - 1), floor - 1, -1):
            if value[index - 1] not in _WEAK_CHARS:
                continue
            if self._inside(index, protected):
                continue
            if value[index - 1].isdigit() and value[index : index + 1].isdigit():
                continue
            if not self._balanced(value[:index]):
                continue
            return index
        for index in range(min(limit, len(value) - 1), floor - 1, -1):
            if not self._inside(index, protected) and not (
                value[index - 1].isdigit() and value[index : index + 1].isdigit()
            ):
                return index
        return min(limit, len(value))

    def safe_boundary_positions(self, value: str) -> frozenset[int]:
        """Return lossless cut positions that do not split protected syntax."""

        text = str(value)
        protected = [(match.start(), match.end()) for match in _URL.finditer(text)]
        protected.extend(
            (match.start(), match.end()) for match in _MARKDOWN_LINK.finditer(text)
        )
        boundaries = {0, len(text)}
        in_fence = False
        fence_start: int | None = None
        line_start = 0
        for line in text.splitlines(keepends=True):
            line_end = line_start + len(line)
            if line.lstrip().startswith("```"):
                if not in_fence:
                    in_fence = True
                    fence_start = line_start
                else:
                    in_fence = False
                    protected.append((fence_start or 0, line_end))
                    fence_start = None
            elif not in_fence and line_end < len(text):
                boundaries.add(line_end)
            line_start = line_end
        if in_fence:
            protected.append((fence_start or 0, len(text)))
        for index, character in enumerate(text, start=1):
            if character not in "。！？!?；;，,：: \n":
                continue
            if self._inside(index, protected):
                continue
            if character == "\n" and index < len(text):
                next_line = text[index:].lstrip(" \t")
                if next_line.startswith(("```", ")", "]")):
                    continue
            if self._balanced(text[:index]):
                boundaries.add(index)
        return frozenset(boundaries)

    @staticmethod
    def _inside(index: int, spans: list[tuple[int, int]]) -> bool:
        return any(start < index < end for start, end in spans)

    @staticmethod
    def _balanced(value: str) -> bool:
        pairs = (("（", "）"), ("(", ")"), ("【", "】"), ("[", "]"))
        return all(value.count(left) == value.count(right) for left, right in pairs)

    def _best_merge_index(self, packed: list[str]) -> int | None:
        candidates = [
            (len(packed[index]) + 2 + len(packed[index + 1]), index)
            for index in range(len(packed) - 1)
            if len(packed[index]) + 2 + len(packed[index + 1]) <= self.max_chars
        ]
        return min(candidates)[1] if candidates else None

    def _capacity_partition(self, value: str) -> tuple[str, ...]:
        remaining = value.strip()
        result: list[str] = []
        while remaining and len(result) < self.max_segments:
            slots_left = self.max_segments - len(result)
            if len(remaining) <= self.max_chars:
                result.append(remaining)
                remaining = ""
                break
            target = min(
                self.max_chars,
                max(self.target_chars, (len(remaining) + slots_left - 1) // slots_left),
            )
            cut = self._safe_cut(remaining, target)
            result.append(remaining[:cut].strip())
            remaining = remaining[cut:].strip()
        if remaining:
            return (value,)
        return tuple(item for item in result if item)
