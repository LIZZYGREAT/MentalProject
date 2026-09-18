"""Read-only GitHub API client; no credential or mutation endpoint is used."""

from __future__ import annotations

import base64
from typing import Any
from urllib.parse import quote

import json
import os
from urllib.parse import urlencode

from .http_client import PublicHttpClient


class GitHubPublicClient:
    API = "https://api.github.com"

    def __init__(self, *, timeout_seconds: float = 10.0, token: str | None = None) -> None:
        self.timeout_seconds = max(0.5, float(timeout_seconds))
        self.token = (token if token is not None else os.environ.get("RESEARCH_GITHUB_TOKEN", "")).strip()
        self.http = PublicHttpClient(timeout_seconds=self.timeout_seconds, max_bytes=4 * 1024 * 1024)

    def _get(self, path: str, **params: Any) -> Any:
        if not path.startswith("/") or any(token in path for token in ("..", "//")):
            raise ValueError("invalid GitHub API path")
        query = urlencode([(key, value) for key, value in params.items() if value is not None])
        response = self.http.fetch(self.API + path + (f"?{query}" if query else ""), extra_headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "MindFlowResearch/1.0",
            **({"Authorization": f"Bearer {self.token}"} if self.token else {}),
        })
        if response.status_code == 404:
            raise ValueError("github_not_found")
        return json.loads(response.body.decode("utf-8", "replace"))

    def search_repositories(self, query: str, *, per_page: int = 10, sort: str = "updated") -> dict[str, Any]:
        if not query or len(query) > 256:
            raise ValueError("invalid GitHub query")
        return self._get("/search/repositories", q=query, sort=sort, per_page=max(1, min(per_page, 20)))

    def repository(self, owner: str, repo: str) -> dict[str, Any]:
        self._validate_name(owner, repo)
        return self._get(f"/repos/{owner}/{repo}")

    def readme(self, owner: str, repo: str) -> dict[str, Any]:
        self._validate_name(owner, repo)
        payload = self._get(f"/repos/{owner}/{repo}/readme")
        encoded = payload.get("content", "")
        if payload.get("encoding") == "base64":
            content = base64.b64decode(encoded).decode("utf-8", "replace")
        else:
            content = str(encoded)
        return {"name": payload.get("name"), "html_url": payload.get("html_url"), "content": content[:60000]}

    def releases(self, owner: str, repo: str, *, per_page: int = 10) -> list[dict[str, Any]]:
        self._validate_name(owner, repo)
        return self._get(f"/repos/{owner}/{repo}/releases", per_page=max(1, min(per_page, 20)))

    def commits(self, owner: str, repo: str, *, per_page: int = 10) -> list[dict[str, Any]]:
        self._validate_name(owner, repo)
        return self._get(f"/repos/{owner}/{repo}/commits", per_page=max(1, min(per_page, 20)))

    @staticmethod
    def _validate_name(owner: str, repo: str) -> None:
        if not owner or not repo or any(not item.replace("-", "").replace("_", "").isalnum() for item in (owner, repo)):
            raise ValueError("invalid GitHub repository")
