"""Reviewed, fixed-endpoint public API adapter."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from urllib.parse import quote_plus

from .contracts import ResearchCandidate
from .http_client import PublicHttpClient


class HackerNewsPublicApi:
    SEARCH_ENDPOINT = "https://hn.algolia.com/api/v1/search_by_date"

    def __init__(self, http: PublicHttpClient) -> None:
        self.http = http

    def search(self, query: str, *, max_results: int = 10) -> list[dict[str, str]]:
        url = f"{self.SEARCH_ENDPOINT}?query={quote_plus(query[:200])}&tags=story&hitsPerPage={max(1, min(int(max_results), 20))}"
        response = self.http.fetch(url)
        payload = __import__("json").loads(response.body.decode("utf-8", "replace"))
        results: list[dict[str, str]] = []
        for hit in payload.get("hits", []):
            object_id = str(hit.get("objectID") or "")
            title = " ".join(str(hit.get("title") or hit.get("story_title") or "").split())
            if not object_id or not title:
                continue
            api_url = f"https://hn.algolia.com/api/v1/items/{quote_plus(object_id)}"
            results.append(ResearchCandidate(
                candidate_id=hashlib.sha256(api_url.encode("utf-8")).hexdigest()[:32],
                source_kind="api", title=title[:300], url=api_url,
                snippet=" ".join(str(hit.get("story_text") or "").split())[:500],
                discovered_at=datetime.now(timezone.utc).isoformat(),
            ).as_dict() | {
                "published_at": hit.get("created_at"),
                "updated_at": hit.get("updated_at"),
            })
        return results
