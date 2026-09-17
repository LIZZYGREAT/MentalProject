"""Small, bounded extractor for the public DOM used by WeChat articles."""

from __future__ import annotations

from html.parser import HTMLParser
import re
from typing import Any


class _WeChatContentParser(HTMLParser):
    _TARGET_IDS = frozenset({"js_content", "rich_media_content", "js_article"})
    _IGNORED_TAGS = frozenset({"script", "style", "noscript", "svg"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._depth = 0
        self._skip_depth = 0
        self._target_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        name = str(tag).casefold()
        if self._skip_depth:
            self._skip_depth += 1
            return
        if name in self._IGNORED_TAGS:
            self._skip_depth = 1
            return
        self._depth += 1
        values = {str(key).casefold(): str(value or "") for key, value in attrs}
        if values.get("id", "").casefold() in self._TARGET_IDS:
            self._target_depth = self._depth
        if self._target_depth and name in {"p", "div", "section", "article", "br", "li", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        name = str(tag).casefold()
        if self._skip_depth:
            self._skip_depth -= 1
            return
        if self._target_depth == self._depth:
            self._target_depth = 0
        self._depth = max(0, self._depth - 1)
        if self._target_depth and name in {"p", "div", "section", "article", "li", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._target_depth and not self._skip_depth:
            value = re.sub(r"\s+", " ", str(data)).strip()
            if value:
                self.parts.append(value)


class WeChatArticleExtractor:
    """Classify and extract only known public article containers."""

    CHALLENGE_MARKERS = (
        "验证码",
        "安全验证",
        "访问异常",
        "请完成验证",
        "captcha",
        "challenge",
        "verify you are human",
        "robot check",
    )

    @classmethod
    def classify(cls, markup: str) -> str:
        lowered = str(markup or "").casefold()
        if any(marker.casefold() in lowered for marker in cls.CHALLENGE_MARKERS):
            return "challenge"
        has_target = bool(re.search(
            r"(?is)(?:id|class)\s*=\s*[\"'][^\"']*(?:js_content|rich_media_content|js_article)[^\"']*[\"']",
            lowered,
        ))
        visible = re.sub(r"(?is)<script.*?</script>|<style.*?</style>|<[^>]+>", " ", lowered)
        visible = re.sub(r"\s+", " ", visible).strip()
        has_shell = bool(
            re.search(
                r"(?is)<(?:div|main)[^>]+(?:id|class)\s*=\s*[\"'][^\"']*(?:app|root|page)[^\"']*[\"']",
                lowered,
            )
            and "<script" in lowered
            and len(visible) < 160
        )
        if has_shell and not has_target:
            return "javascript_shell"
        return "article" if has_target else "unknown"

    @classmethod
    def extract(cls, markup: str, *, max_chars: int = 60000) -> str | None:
        parser = _WeChatContentParser()
        try:
            parser.feed(str(markup or ""))
            parser.close()
        except Exception:
            return None
        text = re.sub(r"[ \t]+", " ", "".join(parser.parts))
        text = "\n".join(line.strip() for line in text.splitlines() if line.strip()).strip()
        return text[:max_chars] or None
