"""HTTP RPC surface for the isolated research runtime."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import tempfile
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .browser import UnavailableBrowser
from .contracts import ResearchEvidenceItem, ResearchJobSpec, canonicalize_url, evidence_envelope
from .executor import SafeExecutor
from .github import GitHubPublicClient
from .http_client import PublicHttpClient
from .policy import ExecPolicy
from .search import PublicSearchClient


class ResearchRuntime:
    def __init__(self, *, workspace: str | Path | None = None, token: str | None = None) -> None:
        if workspace is None:
            workspace = os.environ.get("RESEARCH_WORKSPACE") or (
                "/workspace" if os.name != "nt" else str(Path(tempfile.gettempdir()) / "mindflow-research-workspace")
            )
        self.workspace = Path(workspace).resolve()
        self.token = token if token is not None else os.environ.get("RESEARCH_RUNTIME_TOKEN", "")
        self.http = PublicHttpClient()
        self.executor = SafeExecutor(ExecPolicy(self.workspace))
        self.browser = UnavailableBrowser()
        self.github = GitHubPublicClient()
        self.search_client = PublicSearchClient(self.http)

    def _job(self, raw: dict[str, Any]) -> ResearchJobSpec:
        return ResearchJobSpec.from_mapping(raw)

    def search(self, raw: dict[str, Any]) -> dict[str, Any]:
        job = self._job(raw)
        query = " ".join((job.topic, *job.query_hints))[:500]
        results = self.search_client.search(query, max_results=min(10, job.max_pages))
        return {"ok": True, "topic": job.topic, "results": results}

    def open_url(self, raw: dict[str, Any]) -> dict[str, Any]:
        job = self._job(raw)
        url = canonicalize_url(str(raw.get("url", "")))
        response, document = self.http.read_document(url)
        item = ResearchEvidenceItem.build(
            source_kind="web",
            title=document.title or url,
            canonical_url=document.canonical_url or response.url,
            content=document.text,
            extraction_mode=document.extraction_mode,
            freshness_hours=job.freshness_hours,
            publisher=(url.split("/", 3)[2] if "/" in url else None),
        )
        return {"ok": True, "evidence": item.as_dict(), "content": evidence_envelope([item])}

    async def browser_open(self, raw: dict[str, Any]) -> dict[str, Any]:
        job = self._job(raw)
        url = canonicalize_url(str(raw.get("url", "")))
        result = await self.browser.open(url, wait_seconds=min(float(raw.get("wait_seconds", 2)), 10.0))
        return {"ok": result.ok, "topic": job.topic, **result.__dict__}

    async def exec_public(self, raw: dict[str, Any]) -> dict[str, Any]:
        job = self._job(raw)
        argv = raw.get("argv")
        if not isinstance(argv, list):
            raise ValueError("argv must be a list")
        result = await self.executor.run(argv, timeout_seconds=min(int(raw.get("timeout_seconds", 30)), job.deadline_seconds))
        return {"ok": result.return_code == 0 and not result.timed_out, **result.__dict__}

    def github_read(self, raw: dict[str, Any]) -> dict[str, Any]:
        job = self._job(raw)
        action = str(raw.get("action", "search_repositories"))
        if action == "search_repositories":
            result = self.github.search_repositories(str(raw.get("query", job.topic)))
        else:
            owner, repo = str(raw.get("owner", "")), str(raw.get("repo", ""))
            if action == "repository":
                result = self.github.repository(owner, repo)
            elif action == "readme":
                result = self.github.readme(owner, repo)
            elif action == "releases":
                result = self.github.releases(owner, repo)
            elif action == "commits":
                result = self.github.commits(owner, repo)
            else:
                raise ValueError("unsupported GitHub read action")
        return {"ok": True, "source_kind": "github", "topic": job.topic, "result": result}


def create_app(runtime: ResearchRuntime | None = None) -> Starlette:
    service = runtime or ResearchRuntime()

    async def guard(request: Request) -> JSONResponse | None:
        if service.token and request.headers.get("x-research-token") != service.token:
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        return None

    async def health(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True, "service": "research-runtime"})

    async def dispatch(request: Request, operation: str) -> JSONResponse:
        denied = await guard(request)
        if denied is not None:
            return denied
        try:
            raw = await request.json()
            if not isinstance(raw, dict):
                raise ValueError("request body must be an object")
            result = getattr(service, operation)(raw)
            if asyncio.iscoroutine(result):
                result = await result
            return JSONResponse(result)
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        except Exception:
            return JSONResponse({"ok": False, "error": "research_runtime_failure"}, status_code=502)

    return Starlette(routes=[
        Route("/health", health, methods=["GET"]),
        Route("/v1/research/search", lambda request: dispatch(request, "search"), methods=["POST"]),
        Route("/v1/research/open-url", lambda request: dispatch(request, "open_url"), methods=["POST"]),
        Route("/v1/research/browser-open", lambda request: dispatch(request, "browser_open"), methods=["POST"]),
        Route("/v1/research/exec", lambda request: dispatch(request, "exec_public"), methods=["POST"]),
        Route("/v1/research/github", lambda request: dispatch(request, "github_read"), methods=["POST"]),
    ])


app = create_app()
