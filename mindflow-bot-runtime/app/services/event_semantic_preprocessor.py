"""Rule-first event semantics with optional, durable DeepSeek enrichment."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import time
from typing import Any, Awaitable, Callable, Mapping

from app.repositories import EventSemanticCacheRepository
from services.event_semantic_prompt import PROMPT_VERSION
from services.event_semantics import (
    DIMENSIONS,
    FUSION_POLICY_VERSION,
    RULE_VERSION,
    SEMANTIC_SCHEMA_VERSION,
    OpenAICompatibleSemanticClient,
    fuse_rule_and_external,
    infer_rule_semantics,
    validate_external_semantics,
)
from utils.description_score import score_description
from services.semantic_model_inputs import semantic_model_inputs


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _duration_minutes(event: Mapping[str, Any]) -> float:
    try:
        start = datetime.fromisoformat(str(event.get("start_time") or "").replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(event.get("end_time") or "").replace("Z", "+00:00"))
        return max(0.0, (end - start).total_seconds() / 60.0)
    except (TypeError, ValueError):
        return 60.0


class EventSemanticPreprocessor:
    def __init__(
        self,
        cache: EventSemanticCacheRepository,
        *,
        client: OpenAICompatibleSemanticClient | None,
        model: str,
        max_concurrency: int = 2,
        circuit_seconds: float = 60.0,
    ):
        self.cache = cache
        self.client = client
        self.model = model or "rules-only"
        self._semaphore = asyncio.Semaphore(max(1, max_concurrency))
        self._inflight: dict[tuple[str, str], asyncio.Task] = {}
        self._inflight_lock = asyncio.Lock()
        self._completion_tasks: set[asyncio.Task] = set()
        self._closing = False
        self._circuit_until = 0.0
        self._circuit_seconds = max(1.0, circuit_seconds)

    def _fingerprint(self, event: Mapping[str, Any]) -> str:
        payload = {
            "summary": str(event.get("summary") or "")[:160],
            "description": str(event.get("description") or "")[:800],
            "event_type": str(event.get("event_type") or "task").lower(),
            "task_type": str(event.get("task_type") or "general").lower(),
            "duration_minutes": round(_duration_minutes(event), 2),
            "schema_version": SEMANTIC_SCHEMA_VERSION,
            "prompt_version": PROMPT_VERSION,
            "model": self.model,
        }
        return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()

    def prepare(
        self, participant_id: Any, events: list[dict[str, Any]], *, consent: bool
    ) -> tuple[list[dict[str, Any]], str, str, list[dict[str, Any]]]:
        prepared: list[dict[str, Any]] = []
        misses: list[dict[str, Any]] = []
        external_count = 0
        eligible_count = 0
        for raw in events:
            event = deepcopy(raw)
            event_type = str(event.get("event_type") or "task").lower()
            task_type = str(event.get("task_type") or "general").lower()
            duration = _duration_minutes(event)
            rule_values, matched, floors = infer_rule_semantics(
                name=str(event.get("summary") or ""),
                description=str(event.get("description") or ""),
                event_type=event_type,
                task_type=task_type,
                duration_minutes=duration,
            )
            fingerprint = self._fingerprint(event)
            eligible = event_type not in {"rest", "meal", "nap", "sleep", "gym"}
            eligible_count += int(eligible)
            cached = self.cache.get(
                participant_id, fingerprint, schema_version=SEMANTIC_SCHEMA_VERSION,
                prompt_version=PROMPT_VERSION, model=self.model,
            ) if consent and eligible and self.client else None
            values = dict(rule_values)
            external = None
            source = "rules"
            confidence = 0.82 if matched else 0.68
            if cached:
                external = dict(cached.get("external") or {})
                raw_external_values = external.get("objective_semantics")
                if isinstance(raw_external_values, Mapping):
                    values, _ = fuse_rule_and_external(
                        rule_values, raw_external_values,
                        float(external.get("confidence") or 0.0), floors,
                    )
                    confidence = max(confidence, float(external.get("confidence") or 0.0) * 0.85)
                    source = "hybrid"
                    external_count += 1
            fused_appraisal = appraisal = score_description(
                str(event.get("description") or ""), str(event.get("summary") or "")
            )
            if external and external.get("appraisal_score_1_10") is not None:
                try:
                    external_appraisal = max(1.0, min(10.0, float(external["appraisal_score_1_10"])))
                    weight = min(0.30, 0.30 * float(external.get("confidence") or 0.0))
                    fused_appraisal = max(
                        1.0, min(10.0, appraisal + max(-1.2, min(1.2, weight * (external_appraisal - appraisal))))
                    )
                except (TypeError, ValueError):
                    fused_appraisal = appraisal
            if not cached and consent and eligible and self.client:
                misses.append({
                    "fingerprint": fingerprint,
                    "event": {
                        "summary": str(event.get("summary") or "")[:160],
                        "description": str(event.get("description") or "")[:800],
                        "event_type": event_type,
                        "task_type": task_type,
                        "duration_minutes": round(duration, 2),
                    },
                    "rule_values": rule_values,
                    "floors": floors,
                })
            semantic = {
                "schema_version": SEMANTIC_SCHEMA_VERSION,
                "fingerprint": fingerprint,
                "rule_version": RULE_VERSION,
                "fusion_policy_version": FUSION_POLICY_VERSION,
                "rule": {
                    "appraisal_score_1_10": appraisal,
                    "objective_semantics": {key: round(rule_values[key], 6) for key in DIMENSIONS},
                    "matched_rules": matched,
                },
                "external": external,
                "fused": {
                    "appraisal_score_1_10": round(fused_appraisal, 6),
                    "objective_semantics": {key: round(values[key], 6) for key in DIMENSIONS},
                },
                "values": {key: round(values[key], 6) for key in DIMENSIONS},
                "confidence": round(confidence, 6),
                "source": source,
            }
            metadata = dict(event.get("metadata") or {})
            metadata["semantic"] = semantic
            event["metadata"] = metadata
            prepared.append(event)
        semantic_revision = hashlib.sha256(_canonical([
            {
                "id": item.get("id"),
                "model_inputs": semantic_model_inputs(
                    (item.get("metadata") or {}).get("semantic")
                ),
            }
            for item in prepared
        ]).encode("utf-8")).hexdigest()
        if not eligible_count or external_count == eligible_count:
            status = "hybrid_complete" if external_count else "rules_only"
        elif external_count:
            status = "hybrid_partial"
        else:
            status = "rules_only"
        return prepared, semantic_revision, status, misses

    async def enqueue(
        self, participant_id: Any, misses: list[dict[str, Any]],
        on_complete: Callable[[], Awaitable[None]],
    ) -> None:
        if (
            self._closing or not self.client or not misses
            or time.monotonic() < self._circuit_until
        ):
            return
        tasks = []
        async with self._inflight_lock:
            for miss in misses:
                key = (str(participant_id), miss["fingerprint"])
                task = self._inflight.get(key)
                if task is None or task.done():
                    task = asyncio.create_task(
                        self._enrich_guarded(key, participant_id, miss),
                        name=f"semantic-{miss['fingerprint'][:10]}",
                    )
                    self._inflight[key] = task
                tasks.append(task)
        if tasks:
            async def finish() -> None:
                results = await asyncio.gather(*tasks, return_exceptions=True)
                if any(result is True for result in results):
                    await on_complete()
            completion = asyncio.create_task(
                finish(), name="semantic-enrichment-completion"
            )
            self._completion_tasks.add(completion)
            completion.add_done_callback(self._completion_done)

    def _completion_done(self, task: asyncio.Task) -> None:
        self._completion_tasks.discard(task)
        if task.cancelled():
            return
        # Retrieve callback failures so a completed background task never
        # produces an unobserved "Task exception was never retrieved" warning.
        task.exception()

    async def _enrich_guarded(
        self, key: tuple[str, str], participant_id: Any, miss: dict[str, Any]
    ) -> bool:
        try:
            return await self._enrich_one(participant_id, miss)
        finally:
            current = asyncio.current_task()
            async with self._inflight_lock:
                if self._inflight.get(key) is current:
                    self._inflight.pop(key, None)

    async def close(self, timeout_seconds: float = 10.0) -> None:
        """Stop accepting work and boundedly retrieve all background tasks."""

        self._closing = True
        async with self._inflight_lock:
            tasks = list(self._inflight.values())
        tasks.extend(self._completion_tasks)
        if not tasks:
            return
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=max(0.1, timeout_seconds),
            )
        except asyncio.TimeoutError:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _enrich_one(self, participant_id: Any, miss: dict[str, Any]) -> bool:
        if time.monotonic() < self._circuit_until:
            return False
        async with self._semaphore:
            try:
                raw = await asyncio.to_thread(self.client.infer, miss["event"])
                values, confidence, tags, reasoning = validate_external_semantics(raw)
                appraisal = raw.get("appraisal_score_1_10", 5.0)
                try:
                    appraisal = max(1.0, min(10.0, float(appraisal)))
                except (TypeError, ValueError):
                    appraisal = 5.0
                assessment = {
                    "external": {
                        "available": True,
                        "objective_semantics": values,
                        "appraisal_score_1_10": appraisal,
                        "confidence": confidence,
                        "evidence_tags": tags,
                        "reasoning_summary": reasoning,
                        "model": self.model,
                        "prompt_version": PROMPT_VERSION,
                    }
                }
                await asyncio.to_thread(
                    self.cache.put, participant_id, miss["fingerprint"], assessment,
                    schema_version=SEMANTIC_SCHEMA_VERSION,
                    prompt_version=PROMPT_VERSION, model=self.model,
                )
                return True
            except Exception:
                self._circuit_until = time.monotonic() + self._circuit_seconds
                return False
