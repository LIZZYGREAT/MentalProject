"""Minimal public search discovery through DuckDuckGo's HTML endpoint."""

from __future__ import annotations

from html import unescape
import re
from urllib.parse import parse_qs, quote_plus, urlsplit

from .http_client import PublicHttpClient


_RESULT = re.compile(
    r'<a[^>]+class=["\'][^"\']*result__a[^"\']*["\'][^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_TAG = re.compile(r"<[^>]+>")


class PublicSearchClient:
    def __init__(self, http: PublicHttpClient) -> None:
        self.http = http

    def search(self, query: str, *, max_results: int = 10) -> list[dict[str, str]]:
        endpoint = "https://html.duckduckgo.com/html/?q=" + quote_plus(query[:500])
        response = self.http.fetch(endpoint)
        html = response.body.decode("utf-8", "replace")
        results: list[dict[str, str]] = []
        seen: set[str] = set()
        for raw_url, raw_title in _RESULT.findall(html):
            url = unescape(raw_url)
            parsed = urlsplit(url)
            if parsed.path.startswith("/l/"):
                url = parse_qs(parsed.query).get("uddg", [""])[0]
                parsed = urlsplit(url)
            if parsed.scheme.casefold() != "https" or not parsed.netloc:
                continue
            canonical = parsed._replace(fragment="").geturl()
            if canonical in seen:
                continue
            seen.add(canonical)
            title = _TAG.sub("", unescape(raw_title))
            results.append({"title": " ".join(title.split())[:300], "url": canonical, "snippet": ""})
            if len(results) >= max(1, min(int(max_results), 10)):
                break
        return results

