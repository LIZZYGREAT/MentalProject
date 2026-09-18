"""Read-only GitHub API client; no credential or mutation endpoint is used."""

from __future__ import annotations

import base64
from typing import Any
from urllib.parse import quote

import httpx


class GitHubPublicClient:
    API = "https://api.github.com"

    def __init__(self, *, timeout_seconds: float = 10.0) -> None:
        self.timeout_seconds = max(0.5, float(timeout_seconds))

    def _get(self, path: str, **params: Any) -> Any:
        if not path.startswith("/") or any(token in path for token in ("..", "//")):
            raise ValueError("invalid GitHub API path")
        with httpx.Client(timeout=self.timeout_seconds, trust_env=False, headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "MindFlowResearch/1.0",
        }) as client:
            response = client.get(self.API + path, params=params)
        if response.status_code == 404:
            raise ValueError("github_not_found")
        if response.status_code == 403 and response.headers.get("x-ratelimit-remaining") == "0":
            raise ValueError("github_rate_limited")
        if response.status_code >= 400:
            raise ValueError("github_request_failed")
        return response.json()

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

