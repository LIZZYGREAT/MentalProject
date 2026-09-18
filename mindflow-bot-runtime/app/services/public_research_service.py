"""Participant-safe facade over the isolated public research runtime."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from typing import Any, Mapping, Protocol

import httpx

from app.contracts.research import ResearchEvidenceItem, ResearchJobSpec, evidence_envelope
from app.services.web_search_service import SearchQueryRequiresPublicTopic


logger = logging.getLogger(__name__)


class ResearchGateway(Protocol):
    async def search(self, job: ResearchJobSpec) -> dict[str, Any]: ...
    async def open_url(self, job: ResearchJobSpec, url: str) -> dict[str, Any]: ...
    async def browser_open(self, job: ResearchJobSpec, url: str) -> dict[str, Any]: ...
    async def exec_public(self, job: ResearchJobSpec, argv: list[str], timeout_seconds: int) -> dict[str, Any]: ...
    async def github(self, job: ResearchJobSpec, payload: dict[str, Any]) -> dict[str, Any]: ...


class ResearchRuntimeUnavailable(RuntimeError):
    pass


class ResearchRuntimeClient:
    def __init__(self, base_url: str, *, token: str = "", timeout_seconds: float = 30.0) -> None:
        self.base_url = str(base_url).rstrip("/")
        self.token = str(token)
        self.timeout_seconds = max(1.0, float(timeout_seconds))

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {"x-research-token": self.token} if self.token else {}
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds, trust_env=False) as client:
                response = await client.post(f"{self.base_url}{path}", json=payload, headers=headers)
            data = response.json()
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise ResearchRuntimeUnavailable("runtime_unavailable") from exc
        if response.status_code >= 400 or not isinstance(data, dict):
            raise ResearchRuntimeUnavailable(str(data.get("error") or "runtime_unavailable") if isinstance(data, dict) else "runtime_unavailable")
        return data

    async def search(self, job: ResearchJobSpec) -> dict[str, Any]:
        return await self._post("/v1/research/search", job.as_public_payload())

    async def open_url(self, job: ResearchJobSpec, url: str) -> dict[str, Any]:
        return await self._post("/v1/research/open-url", job.as_public_payload() | {"url": url})

    async def browser_open(self, job: ResearchJobSpec, url: str) -> dict[str, Any]:
        return await self._post("/v1/research/browser-open", job.as_public_payload() | {"url": url})

    async def exec_public(self, job: ResearchJobSpec, argv: list[str], timeout_seconds: int) -> dict[str, Any]:
        return await self._post("/v1/research/exec", job.as_public_payload() | {"argv": argv, "timeout_seconds": timeout_seconds})

    async def github(self, job: ResearchJobSpec, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._post("/v1/research/github", job.as_public_payload() | payload)


class PublicResearchService:
    def __init__(self, *, gateway: ResearchGateway | None, web_search: Any, web_documents: Any, evidence: Any | None = None, audit: Any | None = None) -> None:
        self.gateway = gateway
        self.web_search = web_search
        self.web_documents = web_documents
        self.evidence = evidence
        self.audit = audit

    async def search(self, participant_id: Any, *, topic: str, query_hints: list[str] | tuple[str, ...] = (), freshness_hours: int = 24, source_kinds: list[str] | tuple[str, ...] = ("web",), max_results: int = 5) -> dict[str, Any]:
        job = ResearchJobSpec(topic=topic, query_hints=tuple(query_hints), freshness_hours=int(freshness_hours), source_kinds=tuple(source_kinds), max_pages=max(1, min(int(max_results) + 2, 12)))
        query = " ".join((job.topic, *job.query_hints))[:500]
        audit_id = self.audit.start_job(participant_id, topic=job.topic, query=query, source_kind=job.source_kinds[0]) if self.audit is not None else None
        try:
            if self.gateway is not None:
                result = await self.gateway.search(job)
            else:
                freshness = "day" if job.freshness_hours <= 24 else "week" if job.freshness_hours <= 168 else "month"
                result = await self.web_search.search(participant_id, query=query, freshness=freshness, max_results=max_results)
            items = self._items_from_result(result, topic_label=job.topic, freshness_hours=job.freshness_hours)
            items = self._persist(participant_id, items, topic_label=job.topic)
            if self.audit is not None:
                self.audit.finish_job(audit_id, status="succeeded", request_count=1, page_count=len(items))
            return {"ok": True, "verified": True, "topic": job.topic, "results": [item.as_dict() for item in items], "evidence": evidence_envelope(items)}
        except (ResearchRuntimeUnavailable, SearchQueryRequiresPublicTopic, ValueError) as exc:
            if self.audit is not None and audit_id:
                self.audit.finish_job(audit_id, status="failed", failure_reason=type(exc).__name__)
            return {"ok": False, "error": "research_unavailable", "reason_code": str(exc)[:64], "verified": False}
        except Exception as exc:
            logger.exception("public_research_search_failed")
            if self.audit is not None and audit_id:
                self.audit.finish_job(audit_id, status="failed", failure_reason=type(exc).__name__)
            return {"ok": False, "error": "research_unavailable", "reason_code": "provider_unavailable", "verified": False}

    async def open_url(self, participant_id: Any, *, url: str, topic: str = "public research", freshness_hours: int = 24) -> dict[str, Any]:
        job = ResearchJobSpec(topic=topic, freshness_hours=freshness_hours, source_kinds=("web",))
        try:
            result = await self.gateway.open_url(job, url) if self.gateway is not None else await self.web_documents.read_url(participant_id, url=url)
            items = self._items_from_result(result, topic_label=topic, freshness_hours=freshness_hours)
            items = self._persist(participant_id, items, topic_label=topic)
            if not items:
                return result
            return {"ok": True, "verified": True, "evidence": items[0].as_dict(), "content": evidence_envelope(items)}
        except Exception as exc:
            return {"ok": False, "error": "research_unavailable", "reason_code": str(exc)[:64], "verified": False}

    async def browser_open(self, participant_id: Any, *, url: str, topic: str = "public research") -> dict[str, Any]:
        if self.gateway is None:
            return {"ok": False, "error": "research_runtime_unavailable", "verified": False}
        try:
            return await self.gateway.browser_open(ResearchJobSpec(topic=topic, source_kinds=("web",)), url)
        except Exception as exc:
            return {"ok": False, "error": "research_runtime_unavailable", "reason_code": str(exc)[:64], "verified": False}

    async def exec_public(self, participant_id: Any, *, topic: str, argv: list[str], timeout_seconds: int = 30) -> dict[str, Any]:
        if self.gateway is None:
            return {"ok": False, "error": "research_runtime_unavailable", "verified": False}
        try:
            job = ResearchJobSpec(topic=topic, source_kinds=("api",), max_exec_calls=1)
            return await self.gateway.exec_public(job, argv, timeout_seconds)
        except Exception as exc:
            return {"ok": False, "error": "research_runtime_unavailable", "reason_code": str(exc)[:64], "verified": False}

    async def github(self, participant_id: Any, *, topic: str, payload: dict[str, Any]) -> dict[str, Any]:
        if self.gateway is None:
            return {"ok": False, "error": "research_runtime_unavailable", "verified": False}
        try:
            return await self.gateway.github(ResearchJobSpec(topic=topic, source_kinds=("github",)), payload)
        except Exception as exc:
            return {"ok": False, "error": "research_runtime_unavailable", "reason_code": str(exc)[:64], "verified": False}

    def _persist(self, participant_id: Any, items: list[ResearchEvidenceItem], *, topic_label: str) -> list[ResearchEvidenceItem]:
        if self.evidence is None:
            return items
        persisted: list[ResearchEvidenceItem] = []
        for item in items:
            self.evidence.save_evidence(participant_id, item, topic_label=topic_label)
            persisted.append(item)
        return persisted

    @staticmethod
    def _items_from_result(result: Mapping[str, Any], *, topic_label: str, freshness_hours: int) -> list[ResearchEvidenceItem]:
        if not result.get("ok"):
            raise ResearchRuntimeUnavailable(str(result.get("reason_code") or result.get("error") or "provider_unavailable"))
        raw_items = result.get("results") or result.get("sources") or []
        if isinstance(raw_items, Mapping):
            raw_items = [raw_items]
        if not isinstance(raw_items, list) and result.get("evidence"):
            raw_items = [result["evidence"]]
        items: list[ResearchEvidenceItem] = []
        for raw in raw_items if isinstance(raw_items, list) else []:
            if not isinstance(raw, Mapping):
                continue
            try:
                items.append(ResearchEvidenceItem.from_mapping(raw, topic_label=topic_label))
            except ValueError:
                continue
        if not items and result.get("evidence") and isinstance(result["evidence"], Mapping):
            items.append(ResearchEvidenceItem.from_mapping(result["evidence"], topic_label=topic_label))
        return items

