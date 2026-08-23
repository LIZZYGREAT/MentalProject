"""Conservative Markdown-to-Feishu-plain-text normalization."""

from __future__ import annotations

import re


_FENCE_LINE = re.compile(r"^\s*```[^\n]*$", re.MULTILINE)
_HEADING = re.compile(r"^(\s{0,3})#{1,6}\s+", re.MULTILINE)
_BLOCKQUOTE = re.compile(r"^(\s{0,3})>\s?", re.MULTILINE)
_UNORDERED_BULLET = re.compile(r"^(\s*)[-+*]\s+(?=\S)", re.MULTILINE)
_ORDERED_BULLET = re.compile(r"^(\s*)\d+[.)]\s+(?=\S)", re.MULTILINE)
_IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+['\"][^'\"]*['\"])?\)")
_LINK = re.compile(r"(?<!!)\[([^\]]+)\]\(([^)\s]+)(?:\s+['\"][^'\"]*['\"])?\)")
_STRONG = re.compile(r"(?<!\\)(\*\*|__)(?=\S)(.+?\S)\1", re.DOTALL)
_EMPHASIS = re.compile(r"(?<![\\\w])([*_])(?=\S)([^\n]*?\S)\1(?!\w)")
_INLINE_CODE = re.compile(r"(?<!`)`([^`\n]+)`(?!`)")
_STRIKETHROUGH = re.compile(r"~~(?=\S)(.+?\S)~~", re.DOTALL)
_HORIZONTAL_RULE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$", re.MULTILINE)
_EXCESS_BLANKS = re.compile(r"\n{3,}")


class MarkdownSanitizer:
    """Remove presentation syntax without deleting ordinary math characters."""

    def sanitize(self, text: str) -> str:
        value = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
        value = _FENCE_LINE.sub("", value)
        value = _HORIZONTAL_RULE.sub("", value)
        value = _HEADING.sub(r"\1", value)
        value = _BLOCKQUOTE.sub(r"\1", value)
        value = _UNORDERED_BULLET.sub(r"\1• ", value)
        value = _ORDERED_BULLET.sub(lambda match: f"{match.group(1)}• ", value)
        value = _IMAGE.sub(self._replace_image, value)
        value = _LINK.sub(self._replace_link, value)
        value = _INLINE_CODE.sub(r"\1", value)
        value = _STRIKETHROUGH.sub(r"\1", value)
        # Repeat because nested emphasis is common (for example ***text***).
        for _ in range(3):
            updated = _STRONG.sub(r"\2", value)
            updated = _EMPHASIS.sub(r"\2", updated)
            if updated == value:
                break
            value = updated
        value = value.replace(r"\*", "*").replace(r"\_", "_")
        value = _EXCESS_BLANKS.sub("\n\n", value)
        return "\n".join(line.rstrip() for line in value.split("\n")).strip()

    @staticmethod
    def _replace_link(match: re.Match[str]) -> str:
        label, url = match.group(1).strip(), match.group(2).strip()
        return f"{label}：{url}" if label and label != url else url

    @staticmethod
    def _replace_image(match: re.Match[str]) -> str:
        label, url = match.group(1).strip(), match.group(2).strip()
        return f"{label}：{url}" if label else url


def sanitize_markdown(text: str) -> str:
    return MarkdownSanitizer().sanitize(text)

