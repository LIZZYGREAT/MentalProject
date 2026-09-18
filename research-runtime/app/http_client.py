"""Public-only HTTP transport with redirect and response bounds."""

from __future__ import annotations

from dataclasses import dataclass
import time
from urllib.parse import urljoin

import httpx

from .extraction import ExtractedDocument, extract
from .policy import validate_public_url


@dataclass(frozen=True)
class PublicResponse:
    url: str
    status_code: int
    content_type: str
    body: bytes
    redirect_count: int


class PublicHttpClient:
    def __init__(self, *, timeout_seconds: float = 10, max_bytes: int = 2 * 1024 * 1024, max_redirects: int = 3) -> None:
        self.timeout_seconds = max(0.5, float(timeout_seconds))
        self.max_bytes = max(1024, int(max_bytes))
        self.max_redirects = max(0, min(int(max_redirects), 10))

    def fetch(self, url: str) -> PublicResponse:
        current, _ = validate_public_url(url)
        headers = {
            "User-Agent": "MindFlowResearch/1.0 (+public-research)",
            "Accept": "text/html, text/plain, application/json, text/markdown;q=0.9, */*;q=0.1",
        }
        with httpx.Client(timeout=self.timeout_seconds, follow_redirects=False, trust_env=False, headers=headers) as client:
            for redirect_count in range(self.max_redirects + 1):
                validate_public_url(current)
                response = client.get(current)
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        break
                    current = urljoin(current, location)
                    continue
                if response.status_code in {401, 403}:
                    raise ValueError("authentication_required")
                if response.status_code >= 400:
                    raise ValueError("fetch_failed")
                if len(response.content) > self.max_bytes:
                    raise ValueError("response_too_large")
                return PublicResponse(
                    url=current,
                    status_code=response.status_code,
                    content_type=response.headers.get("content-type", "text/plain").split(";", 1)[0],
                    body=response.content,
                    redirect_count=redirect_count,
                )
        raise ValueError("too_many_redirects")

    def read_document(self, url: str) -> tuple[PublicResponse, ExtractedDocument]:
        response = self.fetch(url)
        document = extract(response.body, content_type=response.content_type)
        return response, document

