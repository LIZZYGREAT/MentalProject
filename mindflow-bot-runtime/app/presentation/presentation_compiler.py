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
        restrict_body_urls: bool = False,
    ) -> str:
        if mode not in {"rich_markdown", "streaming_markdown"}:
            return str(answer)
        verified_evidence = evidence or PresentationEvidence()
        allowed_urls = (
            self._allowed_urls(verified_evidence) if restrict_body_urls else None
        )
        body = self.markdown.compile(answer, allowed_urls=allowed_urls)
        footer = self._source_footer(verified_evidence)
        return "\n\n".join(part for part in (body, footer) if part)

    @staticmethod
    def _allowed_urls(evidence: PresentationEvidence) -> set[str]:
        return {
            url
            for source in evidence.sources
            if (url := canonical_public_url(source.url)) is not None
        }

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
