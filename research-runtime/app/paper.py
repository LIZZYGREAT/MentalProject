"""Small, explicit arXiv discovery adapter for paper source kinds."""

from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import xml.etree.ElementTree as ET
from urllib.parse import quote_plus

from .contracts import ResearchCandidate
from .http_client import PublicHttpClient


class ArxivPublicClient:
    ENDPOINT = "https://export.arxiv.org/api/query"

    def __init__(self, http: PublicHttpClient) -> None:
        self.http = http

    def search(self, query: str, *, max_results: int = 10) -> list[dict[str, str]]:
        url = f"{self.ENDPOINT}?search_query=all:{quote_plus(query[:200])}&max_results={max(1, min(int(max_results), 20))}&sortBy=submittedDate&sortOrder=descending"
        response = self.http.fetch(url)
        root = ET.fromstring(response.body)
        namespace = {"atom": "http://www.w3.org/2005/Atom"}
        results: list[dict[str, str]] = []
        for entry in root.findall("atom:entry", namespace):
            title = " ".join((entry.findtext("atom:title", "", namespace) or "").split())
            link = next((item.attrib.get("href", "") for item in entry.findall("atom:link", namespace) if item.attrib.get("rel") == "alternate"), "")
            published = entry.findtext("atom:published", "", namespace) or None
            updated = entry.findtext("atom:updated", "", namespace) or None
            if not title or not link.startswith("https://"):
                continue
            results.append(ResearchCandidate(
                candidate_id=hashlib.sha256(link.encode("utf-8")).hexdigest()[:32],
                source_kind="paper",
                title=title[:300],
                url=link,
                snippet=" ".join((entry.findtext("atom:summary", "", namespace) or "").split())[:500],
                discovered_at=datetime.now(timezone.utc).isoformat(),
            ).as_dict() | {"published_at": published, "updated_at": updated})
        return results
