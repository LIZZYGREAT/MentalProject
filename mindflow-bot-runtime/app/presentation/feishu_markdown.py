"""Deterministic compilation of model text into Feishu-safe Markdown."""

from __future__ import annotations

import html
import re
from urllib.parse import urlsplit, urlunsplit


_HTML_TAG = re.compile(r"</?[A-Za-z][^>\n]*>")
_MARKDOWN_LINK = re.compile(r"(!?)\[([^\]\n]+)\]\(([^)\s]+)(?:\s+['\"][^)]*['\"])?\)")
_TABLE_SEPARATOR = re.compile(r"^\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*$")


def canonical_public_url(value: str) -> str | None:
    try:
        parsed = urlsplit(str(value or "").strip())
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not hostname:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    netloc = hostname.casefold()
    if port is not None:
        netloc = f"{netloc}:{port}"
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path or "", parsed.query, ""))


class FeishuMarkdownCompiler:
    """Keep a small mobile-friendly Markdown subset and discard active markup."""

    def compile(self, text: str, *, allow_code: bool = False) -> str:
        value = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
        value = _HTML_TAG.sub("", value)
        value = re.sub(
            r"(?m)^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$",
            lambda match: f"**{match.group(1).strip()}**",
            value,
        )
        value = _MARKDOWN_LINK.sub(self._safe_link, value)
        value = self._flatten_tables(value)
        if not allow_code:
            value = re.sub(r"```[^\n]*\n?", "", value)
            value = value.replace("```", "")
            value = re.sub(r"`([^`\n]+)`", r"\1", value)
        value = re.sub(r"\n{3,}", "\n\n", value)
        return value.strip()

    @staticmethod
    def _safe_link(match: re.Match[str]) -> str:
        label = match.group(2).strip()
        url = canonical_public_url(match.group(3))
        if match.group(1) or url is None:
            return label
        return f"[{label}]({url})"

    @staticmethod
    def _flatten_tables(value: str) -> str:
        lines: list[str] = []
        for line in value.splitlines():
            if _TABLE_SEPARATOR.match(line):
                continue
            stripped = line.strip()
            if stripped.startswith("|") and stripped.endswith("|"):
                cells = [cell.strip() for cell in stripped.strip("|").split("|")]
                cells = [cell for cell in cells if cell]
                if cells:
                    lines.append(f"- {' · '.join(cells)}")
                continue
            lines.append(line)
        return "\n".join(lines)


def safe_source_label(value: str) -> str:
    label = _HTML_TAG.sub("", html.unescape(str(value or "")).strip())
    label = re.sub(r"[\[\]()\n\r]+", " ", label)
    return re.sub(r"\s+", " ", label).strip()[:200] or "来源"
