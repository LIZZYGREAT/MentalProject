"""Deterministic response compiler for rich Feishu presentation modes."""

from __future__ import annotations

from app.presentation.contracts import PresentationEvidence, PresentationMode
from app.presentation.feishu_markdown import (
    FeishuMarkdownCompiler,
    canonical_public_url,
    safe_source_label,
)


class PresentationCompiler:
    def __init__(self, markdown: FeishuMarkdownCompiler | None = None) -> None:
        self.markdown = markdown or FeishuMarkdownCompiler()

    def compile(
        self,
        answer: str,
        *,
        mode: PresentationMode,
        evidence: PresentationEvidence | None = None,
    ) -> str:
        if mode not in {"rich_markdown", "streaming_markdown"}:
            return str(answer)
        body = self.markdown.compile(answer)
        footer = self._source_footer(evidence or PresentationEvidence())
        return "\n\n".join(part for part in (body, footer) if part)

    @staticmethod
    def _source_footer(evidence: PresentationEvidence) -> str:
        entries: list[tuple[str, str]] = []
        seen: set[str] = set()
        for source in evidence.sources:
            url = canonical_public_url(source.url)
            if url is None or url in seen:
                continue
            seen.add(url)
            entries.append((safe_source_label(source.title), url))
            if len(entries) >= 5:
                break
        if not entries:
            return ""
        lines = ["**来源**", ""]
        lines.extend(
            f"{index}. [{title}]({url})"
            for index, (title, url) in enumerate(entries, start=1)
        )
        return "\n".join(lines)
