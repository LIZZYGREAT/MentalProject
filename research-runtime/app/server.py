"""HTTP RPC surface for the isolated research runtime."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .browser import BrowserProvider, PlaywrightBrowserProvider, UnavailableBrowser
from .contracts import ResearchEvidenceItem, ResearchJobSpec, canonicalize_url, evidence_envelope
from .executor import SafeExecutor
from .github import GitHubPublicClient
from .http_client import PublicHttpClient
from .policy import ExecPolicy
from .search import PublicSearchClient
from .paper import ArxivPublicClient
from .sessions import ResearchBudgetExceeded, ResearchJobSession


class ResearchRuntime:
    def __init__(
        self,
        *,
        workspace: str | Path | None = None,
        token: str | None = None,
        browser: BrowserProvider | None = None,
        exec_enabled: bool | None = None,
    ) -> None:
        if workspace is None:
            workspace = os.environ.get("RESEARCH_WORKSPACE") or (
                "/workspace" if os.name != "nt" else str(Path(tempfile.gettempdir()) / "mindflow-research-workspace")
            )
        self.workspace = Path(workspace).resolve()
        self.token = token if token is not None else os.environ.get("RESEARCH_RUNTIME_TOKEN", "")
        self.require_token = os.environ.get("RESEARCH_RUNTIME_REQUIRE_TOKEN", "false").casefold() in {"1", "true", "yes", "on"}
        if self.require_token and not self.token:
            raise RuntimeError("RESEARCH_RUNTIME_TOKEN is required")
        self.http = PublicHttpClient()
        self.executor = SafeExecutor(ExecPolicy(self.workspace))
        self.browser = browser or (PlaywrightBrowserProvider() if os.environ.get("RESEARCH_BROWSER_ENABLED", "true").casefold() in {"1", "true", "yes", "on"} else UnavailableBrowser())
        self.exec_enabled = exec_enabled if exec_enabled is not None else os.environ.get("RESEARCH_EXEC_ENABLED", "false").casefold() in {"1", "true", "yes", "on"}
        self.github = GitHubPublicClient()
        self.search_client = PublicSearchClient(self.http)
        self.paper = ArxivPublicClient(self.http)
        self.sessions: dict[str, ResearchJobSession] = {}

    def _job(self, raw: dict[str, Any]) -> tuple[ResearchJobSpec, ResearchJobSession]:
        job = ResearchJobSpec.from_mapping(raw)
        session = self.sessions.get(job.job_id or "")
        if session is None:
            session = ResearchJobSession.from_spec(job)
            self.sessions[session.job_id] = session
        return job, session

    def search(self, raw: dict[str, Any]) -> dict[str, Any]:
        job, session = self._job(raw)
        query = " ".join((job.topic, *job.query_hints))[:500]
        results: list[dict[str, Any]] = []
        for source_kind in job.source_kinds:
            session.consume("search")
            if source_kind == "web":
                results.extend(self.search_client.search(query, max_results=min(10, job.max_pages)))
            elif source_kind == "github":
                payload = self.github.search_repositories(query, per_page=min(10, job.max_pages))
                results.extend(self._github_candidates(payload))
            elif source_kind == "paper":
                results.extend(self.paper.search(query, max_results=min(10, job.max_pages)))
            elif source_kind == "api":
                raise ValueError("api_source_requires_reviewed_adapter")
        return {"ok": True, "job_id": session.job_id, "topic": job.topic, "results": results[: job.max_pages], "candidate_only": True}

    def open_url(self, raw: dict[str, Any]) -> dict[str, Any]:
        job, session = self._job(raw)
        session.consume("page")
        url = canonicalize_url(str(raw.get("url", "")))
        response, document = self.http.read_document(url)
        if document.extraction_mode == "javascript_shell":
            return {"ok": False, "job_id": session.job_id, "reason_code": "javascript_shell", "candidate_url": url, "verified": False}
        item = ResearchEvidenceItem.build(
            source_kind="web",
            title=document.title or url,
            canonical_url=document.canonical_url or response.url,
            content=document.text,
            extraction_mode=document.extraction_mode,
            freshness_hours=job.freshness_hours,
            publisher=(url.split("/", 3)[2] if "/" in url else None),
            published_at=document.published_at,
            updated_at=document.updated_at,
        )
        return {"ok": True, "job_id": session.job_id, "verified": True, "evidence": item.as_dict(), "content": evidence_envelope([item])}

    async def browser_open(self, raw: dict[str, Any]) -> dict[str, Any]:
        job, session = self._job(raw)
        session.consume("browser")
        url = canonicalize_url(str(raw.get("url", "")))
        result = await self.browser.open(url, wait_seconds=min(float(raw.get("wait_seconds", 2)), 10.0))
        if not result.ok:
            return {"ok": False, "job_id": session.job_id, "topic": job.topic, "verified": False, **result.__dict__}
        item = ResearchEvidenceItem.build(
            source_kind="web",
            title=result.title or result.url,
            canonical_url=result.url,
            content=result.text,
            extraction_mode="browser",
            freshness_hours=job.freshness_hours,
            publisher=(url.split("/", 3)[2] if "/" in url else None),
        )
        return {"ok": True, "job_id": session.job_id, "topic": job.topic, "verified": True, "evidence": item.as_dict(), "links": list(result.links), "content": evidence_envelope([item])}

    async def exec_public(self, raw: dict[str, Any]) -> dict[str, Any]:
        job, session = self._job(raw)
        if not self.exec_enabled:
            raise ValueError("research_exec_disabled")
        session.consume("exec")
        argv = raw.get("argv")
        if not isinstance(argv, list):
            raise ValueError("argv must be a list")
        result = await self.executor.run(argv, timeout_seconds=min(int(raw.get("timeout_seconds", 30)), job.deadline_seconds))
        source_url = canonicalize_url(str(raw.get("source_url", "")))
        if not result.stdout:
            return {"ok": result.return_code == 0 and not result.timed_out, "job_id": session.job_id, "verified": False, **result.__dict__}
        item = ResearchEvidenceItem.build(
            source_kind="api", title=str(raw.get("title") or source_url), canonical_url=source_url,
            content=result.stdout, extraction_mode="exec", freshness_hours=job.freshness_hours,
        )
        return {"ok": result.return_code == 0 and not result.timed_out, "job_id": session.job_id, "verified": True, "evidence": item.as_dict(), "content": evidence_envelope([item]), "stderr": result.stderr, "timed_out": result.timed_out}

    def github_read(self, raw: dict[str, Any]) -> dict[str, Any]:
        job, session = self._job(raw)
        session.consume("page")
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
        evidence = self._github_evidence(action, result, job)
        return {"ok": True, "job_id": session.job_id, "source_kind": "github", "topic": job.topic, "result": result, "evidence": [item.as_dict() for item in evidence], "content": evidence_envelope(evidence)}

    @staticmethod
    def _github_candidates(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
        from datetime import datetime, timezone
        import hashlib
        results = []
        for item in payload.get("items", []) if isinstance(payload, Mapping) else []:
            url = str(item.get("html_url") or "")
            if not url.startswith("https://"):
                continue
            results.append({
                "candidate_id": hashlib.sha256(url.encode("utf-8")).hexdigest()[:32],
                "source_kind": "github", "title": str(item.get("full_name") or item.get("name") or url)[:300],
                "url": url, "snippet": str(item.get("description") or "")[:500],
                "discovered_at": datetime.now(timezone.utc).isoformat(),
                "published_at": item.get("created_at"), "updated_at": item.get("updated_at") or item.get("pushed_at"),
            })
        return results

    @staticmethod
    def _github_evidence(action: str, result: Any, job: ResearchJobSpec) -> list[ResearchEvidenceItem]:
        items: list[ResearchEvidenceItem] = []
        if action == "search_repositories":
            for raw in result.get("items", [])[: job.max_pages]:
                url = str(raw.get("html_url") or "")
                if not url.startswith("https://"):
                    continue
                import json
                items.append(ResearchEvidenceItem.build(
                    source_kind="github", title=str(raw.get("full_name") or raw.get("name") or url), canonical_url=url,
                    content=json.dumps(raw, ensure_ascii=False)[:60000], extraction_mode="api", freshness_hours=job.freshness_hours,
                    publisher="GitHub", published_at=raw.get("created_at"), updated_at=raw.get("updated_at") or raw.get("pushed_at"),
                ))
        elif action == "repository":
            url = str(result.get("html_url") or "")
            if url.startswith("https://"):
                import json
                items.append(ResearchEvidenceItem.build(source_kind="github", title=str(result.get("full_name") or url), canonical_url=url, content=json.dumps(result, ensure_ascii=False)[:60000], extraction_mode="api", freshness_hours=job.freshness_hours, publisher="GitHub", published_at=result.get("created_at"), updated_at=result.get("updated_at") or result.get("pushed_at")))
        elif action == "readme":
            url = str(result.get("html_url") or "")
            if url.startswith("https://"):
                items.append(ResearchEvidenceItem.build(source_kind="github", title=str(result.get("name") or url), canonical_url=url, content=str(result.get("content") or ""), extraction_mode="api", freshness_hours=job.freshness_hours, publisher="GitHub"))
        else:
            for raw in result if isinstance(result, list) else []:
                url = str(raw.get("html_url") or raw.get("url") or "")
                if not url.startswith("https://"):
                    continue
                import json
                published = raw.get("published_at") or raw.get("commit", {}).get("author", {}).get("date")
                items.append(ResearchEvidenceItem.build(source_kind="github", title=str(raw.get("name") or raw.get("sha") or url), canonical_url=url, content=json.dumps(raw, ensure_ascii=False)[:60000], extraction_mode="api", freshness_hours=job.freshness_hours, publisher="GitHub", published_at=published, updated_at=raw.get("updated_at") or published))
        return items


def create_app(runtime: ResearchRuntime | None = None) -> Starlette:
    service = runtime or ResearchRuntime()

    async def guard(request: Request) -> JSONResponse | None:
        if service.require_token and request.headers.get("x-research-token") != service.token:
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
