"""Small, bounded HTML/text extraction for public evidence."""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
import re


@dataclass(frozen=True)
class ExtractedDocument:
    title: str
    text: str
    canonical_url: str | None = None
    extraction_mode: str = "http"
    published_at: str | None = None
    updated_at: str | None = None


class _Parser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title: list[str] = []
        self.body: list[str] = []
        self.canonical_url: str | None = None
        self.published_at: str | None = None
        self.updated_at: str | None = None
        self._title = False
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        name = tag.casefold()
        values = {key.casefold(): value or "" for key, value in attrs}
        if name == "title":
            self._title = True
        if name in {"script", "style", "noscript", "svg"}:
            self._skip += 1
        if name == "link" and "canonical" in values.get("rel", "").casefold().split():
            self.canonical_url = values.get("href") or None
        if name == "meta":
            key = (values.get("property") or values.get("name") or "").casefold()
            content = values.get("content") or None
            if key in {"article:published_time", "datepublished", "datepublished"}:
                self.published_at = content
            elif key in {"article:modified_time", "datemodified", "last-modified"}:
                self.updated_at = content

    def handle_endtag(self, tag: str) -> None:
        name = tag.casefold()
        if name == "title":
            self._title = False
        if name in {"script", "style", "noscript", "svg"} and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if self._title:
            self.title.append(data)
        if not self._skip:
            self.body.append(data)


def extract(content: bytes, *, content_type: str, max_chars: int = 60_000) -> ExtractedDocument:
    charset = "utf-8"
    try:
        text = content.decode(charset, "replace")
    except Exception:
        text = str(content)
    if "html" not in content_type.casefold():
        clean = " ".join(text.split())[:max_chars]
        return ExtractedDocument("", clean, extraction_mode="http")
    parser = _Parser()
    parser.feed(text)
    title = " ".join("".join(parser.title).split())[:300]
    body = " ".join(" ".join(parser.body).split())
    if len(body) < 80 and ("id=\"app\"" in text or "id='app'" in text or "__next" in text):
        return ExtractedDocument(title, body[:max_chars], parser.canonical_url, "javascript_shell", parser.published_at, parser.updated_at)
    return ExtractedDocument(title, body[:max_chars], parser.canonical_url, "http", parser.published_at, parser.updated_at)
