"""Rule-first event semantics with optional, durable DeepSeek enrichment."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import logging
import re
import time
from typing import Any, Awaitable, Callable, Hashable, Mapping

import requests

from app.repositories import EventSemanticCacheRepository
from services.event_semantic_prompt import PROMPT_VERSION
from services.event_semantics import (
    DIMENSIONS,
    FUSION_POLICY_VERSION,
    RULE_VERSION,
    SEMANTIC_SCHEMA_VERSION,
    OpenAICompatibleSemanticClient,
    SemanticContentRejected,
    SemanticError,
    SemanticProviderError,
    fuse_rule_and_external,
    infer_rule_semantics,
    validate_course_match,
    validate_event_classification,
    validate_external_semantics,
)
from services.event_classifier import classify_event, finalize_event_classification
from utils.description_score import score_description
from services.semantic_model_inputs import semantic_model_inputs
from services.workload import WorkloadEstimator


logger = logging.getLogger(__name__)
_WORKLOAD_ESTIMATOR = WorkloadEstimator()
_SECRET = re.compile(
    r"(?i)(bearer\s+)[^\s,;]+|((?:api[_-]?key|secret|token)\s*[=:]\s*)[^\s,;]+"
)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _safe_error_message(exc: Exception) -> str:
    message = " ".join(str(exc).split())[:160]
    return _SECRET.sub(lambda match: f"{match.group(1) or match.group(2)}[redacted]", message)


def _duration_minutes(event: Mapping[str, Any]) -> float:
    if event.get("duration_minutes") is not None:
        try:
            return max(0.0, float(event["duration_minutes"]))
        except (TypeError, ValueError):
            pass
    try:
        start = datetime.fromisoformat(str(event.get("start_time") or "").replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(event.get("end_time") or "").replace("Z", "+00:00"))
        return max(0.0, (end - start).total_seconds() / 60.0)
    except (TypeError, ValueError):
        return 60.0


def _normalized_semantic_event(event: Mapping[str, Any]) -> dict[str, Any]:
    metadata = event.get("metadata") if isinstance(event.get("metadata"), Mapping) else {}
    classification = (
        metadata.get("classification")
        if isinstance(metadata.get("classification"), Mapping)
        else {}
    )
    raw_context = classification.get("course_catalog_context")
    context = dict(raw_context) if isinstance(raw_context, Mapping) else {}
    candidates = []
    for candidate in list(context.get("candidates") or [])[:8]:
        if not isinstance(candidate, Mapping):
            continue
        candidates.append(
            {
                "canonical_name": str(candidate.get("canonical_name") or "")[:200],
                "code": str(candidate.get("code") or "")[:64],
                "credits": candidate.get("credits"),
                "hours": candidate.get("hours"),
                "hours_per_week": candidate.get("hours_per_week"),
                "local_score": candidate.get("local_score"),
            }
        )
    return {
        "summary": str(event.get("summary") or "")[:200],
        "description": str(event.get("description") or "")[:800],
        "event_type": str(event.get("event_type") or "task").lower(),
        "task_type": str(event.get("task_type") or "general").lower(),
        "preliminary_event_type": str(
            classification.get("preliminary_event_type")
            or event.get("event_type")
            or "task"
        ).lower(),
        "preliminary_task_type": str(
            classification.get("preliminary_task_type")
            or event.get("task_type")
            or "general"
        ).lower(),
        "duration_minutes": round(_duration_minutes(event), 2),
        "course_catalog_context": {
            "catalog_revision": str(context.get("catalog_revision") or ""),
            "resolver_version": str(context.get("resolver_version") or ""),
            "query": str(context.get("query") or "")[:300],
            "normalized_query": str(context.get("normalized_query") or "")[:300],
            "expanded_query": str(context.get("expanded_query") or "")[:300],
            "identity_constraints": dict(
                context.get("identity_constraints") or {}
            ),
            "candidates": candidates,
        },
    }


def _ensure_preliminary_classification(event: Mapping[str, Any]) -> dict[str, Any]:
    result = deepcopy(dict(event))
    metadata = dict(result.get("metadata") or {})
    if isinstance(metadata.get("classification"), Mapping):
        return result
    if isinstance(result.get("course_catalog_context"), Mapping):
        metadata["classification"] = {
            "preliminary_event_type": str(
                result.get("preliminary_event_type")
                or result.get("event_type")
                or "task"
            ),
            "preliminary_task_type": str(
                result.get("preliminary_task_type")
                or result.get("task_type")
                or "general"
            ),
            "course_catalog_context": deepcopy(
                dict(result["course_catalog_context"])
            ),
        }
        result["metadata"] = metadata
        return result
    classified = classify_event(
        str(result.get("summary") or result.get("name") or ""),
        str(result.get("description") or ""),
        str(result.get("event_type") or ""),
        str(result.get("task_type") or result.get("level") or ""),
    )
    classification = dict(classified.pop("classification"))
    classified.pop("event_kind", None)
    metadata.update(dict(classified.pop("metadata", {})))
    metadata["classification"] = classification
    result.update(classified)
    result["metadata"] = metadata
    return result


def _component_rejection(exc: SemanticError) -> dict[str, Any]:
    reason = getattr(exc, "reason", None) or type(exc).__name__
    result: dict[str, Any] = {
        "status": "rejected",
        "reason": str(reason)[:128],
    }
    confidence = getattr(exc, "confidence", None)
    if confidence is not None:
        result["confidence"] = float(confidence)
    return result


def _validate_response_components(
    raw: Mapping[str, Any],
    event: Mapping[str, Any],
    *,
    model: str,
) -> tuple[str, dict[str, Any], dict[str, Any] | None]:
    """Validate independent provider components without cascading rejection."""

    objective_values: dict[str, float] | None = None
    objective_confidence = 0.0
    appraisal: float | None = None
    tags: list[str] = []
    reasoning = ""
    try:
        objective_values, objective_confidence, tags, reasoning = (
            validate_external_semantics(raw)
        )
        appraisal = float(raw["appraisal_score_1_10"])
        objective_state: dict[str, Any] = {"status": "complete"}
    except SemanticContentRejected as exc:
        objective_state = _component_rejection(exc)

    try:
        event_classification = validate_event_classification(raw)
        if event_classification is None:
            classification_state = {
                "status": "rejected",
                "reason": "event_classification_missing",
            }
        else:
            classification_state = {"status": "complete"}
    except SemanticError as exc:
        event_classification = None
        classification_state = _component_rejection(exc)

    candidates = list(
        (event.get("course_catalog_context") or {}).get("candidates") or []
    )
    try:
        course_match = validate_course_match(raw, candidates)
        if course_match.get("rejected"):
            course_state = {
                "status": "rejected",
                "reason": str(course_match["rejected"])[:128],
            }
        else:
            course_state = {"status": "complete"}
    except SemanticError as exc:
        course_match = {
            "matched": False,
            "canonical_name": None,
            "code": None,
            "confidence": 0.0,
        }
        course_state = _component_rejection(exc)

    components = {
        "objective": objective_state,
        "classification": classification_state,
        "course_match": course_state,
    }
    material_component = bool(
        objective_values is not None
        or event_classification is not None
        or course_match.get("matched") is True
    )
    any_rejected = any(
        component["status"] == "rejected" for component in components.values()
    )
    status = (
        "rejected"
        if not material_component
        else "partial"
        if any_rejected
        else "complete"
    )
    primary_rejection = next(
        (
            component
            for component in components.values()
            if component["status"] == "rejected"
        ),
        None,
    )
    return status, {
        "available": True,
        "objective_semantics": objective_values,
        "appraisal_score_1_10": appraisal,
        "confidence": objective_confidence,
        "evidence_tags": tags,
        "reasoning_summary": reasoning,
        "event_classification": event_classification,
        "course_match": course_match,
        "components": components,
        "model": model,
        "prompt_version": PROMPT_VERSION,
    }, primary_rejection


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
        self._completion_states: dict[Hashable, dict[str, Any]] = {}
        self._closing = False
        self._circuit_until = 0.0
        self._circuit_seconds = max(1.0, circuit_seconds)

    def _fingerprint(self, event: Mapping[str, Any]) -> str:
        event = _ensure_preliminary_classification(event)
        payload = {
            **_normalized_semantic_event(event),
            "schema_version": SEMANTIC_SCHEMA_VERSION,
            "prompt_version": PROMPT_VERSION,
            "model": self.model,
        }
        return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()

    def _validated_cached_external(
        self,
        participant_id: Any,
        fingerprint: str,
        event: Mapping[str, Any],
    ) -> tuple[str, dict[str, Any] | None]:
        entry = self.cache.get_entry(
            participant_id, fingerprint,
            schema_version=SEMANTIC_SCHEMA_VERSION,
            prompt_version=PROMPT_VERSION, model=self.model,
        )
        if not entry:
            return "missing", None
        if entry["status"] == "rejected":
            return "rejected", None
        if entry["status"] not in {"complete", "partial"}:
            return "missing", None
        cached = entry["assessment"]
        external = dict(cached.get("external") or {})
        components = dict(external.get("components") or {})
        if not components:
            # Cache rows written before component-wise validation contained a
            # fully validated external payload and are still safe to reuse.
            components = {
                "objective": {"status": "complete"},
                "classification": {"status": "complete"},
                "course_match": {"status": "complete"},
            }
        try:
            if (components.get("objective") or {}).get("status") == "complete":
                values, confidence, tags, reasoning = validate_external_semantics({
                    **external, "values": external.get("objective_semantics"),
                })
            else:
                values, confidence, tags, reasoning = None, 0.0, [], ""
            if (components.get("classification") or {}).get("status") == "complete":
                event_classification = validate_event_classification(external)
            else:
                event_classification = None
            if (components.get("course_match") or {}).get("status") == "complete":
                course_match = validate_course_match(
                    external,
                    list(
                        (event.get("course_catalog_context") or {}).get("candidates")
                        or []
                    ),
                )
            else:
                course_match = dict(external.get("course_match") or {})
                course_match.update(
                    {
                        "matched": False,
                        "canonical_name": None,
                        "code": None,
                        "confidence": 0.0,
                    }
                )
        except (SemanticError, TypeError, ValueError):
            return "missing", None
        return entry["status"], {
            **external,
            "objective_semantics": values,
            "confidence": confidence,
            "evidence_tags": tags,
            "reasoning_summary": reasoning,
            "event_classification": event_classification,
            "course_match": course_match,
        }

    def prepare(
        self, participant_id: Any, events: list[dict[str, Any]], *, consent: bool
    ) -> tuple[list[dict[str, Any]], str, str, list[dict[str, Any]]]:
        prepared: list[dict[str, Any]] = []
        misses: list[dict[str, Any]] = []
        external_count = 0
        partial_count = 0
        assisted_count = 0
        eligible_count = 0
        for raw in events:
            event = _ensure_preliminary_classification(raw)
            normalized = _normalized_semantic_event(event)
            fingerprint = self._fingerprint(event)
            eligible = normalized["event_type"] not in {
                "rest", "meal", "nap", "sleep", "gym"
            }
            eligible_count += int(eligible)
            cache_status, external = self._validated_cached_external(
                participant_id, fingerprint, normalized
            ) if consent and eligible and self.client else ("missing", None)
            if external:
                event = finalize_event_classification(
                    event,
                    external_classification=external.get("event_classification"),
                    external_course_match=external.get("course_match"),
                )
                normalized = _normalized_semantic_event(event)
                assisted_count += 1
                partial_count += int(cache_status == "partial")
            event_type = normalized["event_type"]
            task_type = normalized["task_type"]
            duration = normalized["duration_minutes"]
            rule_values, matched, floors = infer_rule_semantics(
                name=normalized["summary"],
                description=normalized["description"],
                event_type=event_type,
                task_type=task_type,
                duration_minutes=duration,
            )
            values = dict(rule_values)
            source = "rules"
            confidence = 0.82 if matched else 0.68
            if external and external.get("objective_semantics") is not None:
                values, _ = fuse_rule_and_external(
                    rule_values, external["objective_semantics"],
                    external["confidence"], floors,
                )
                confidence = max(confidence, external["confidence"] * 0.85)
                source = "hybrid"
                external_count += 1
            fused_appraisal = appraisal = score_description(
                normalized["description"], normalized["summary"]
            )
            if external and external.get("appraisal_score_1_10") is not None:
                external_appraisal = float(external["appraisal_score_1_10"])
                weight = min(0.30, 0.30 * float(external.get("confidence") or 0.0))
                fused_appraisal = max(
                    1.0, min(10.0, appraisal + max(-1.2, min(1.2, weight * (external_appraisal - appraisal))))
                )
            if (
                not external
                and cache_status != "rejected"
                and consent
                and eligible
                and self.client
            ):
                misses.append({
                    "fingerprint": fingerprint,
                    "event": normalized,
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
            workload = _WORKLOAD_ESTIMATOR.estimate(values)
            semantic["workload_feature_vector"] = workload.feature_vector
            semantic["workload_prior"] = (
                0.0
                if event_type in {"rest", "meal", "nap", "sleep"}
                else workload.workload_prior
            )
            semantic["workload_schema_version"] = workload.schema_version
            semantic["workload_model_version"] = workload.model_version
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
                "classification": {
                    key: item.get(key)
                    for key in (
                        "event_type",
                        "task_type",
                        "course_name",
                        "course_code",
                        "related_course_name",
                        "related_course_code",
                        "course_match_source",
                        "course_catalog_revision",
                    )
                },
            }
            for item in prepared
        ]).encode("utf-8")).hexdigest()
        if (
            not eligible_count
            or (external_count == eligible_count and partial_count == 0)
        ):
            status = "hybrid_complete" if external_count else "rules_only"
        elif external_count or assisted_count:
            status = "hybrid_partial"
        else:
            status = "rules_only"
        return prepared, semantic_revision, status, misses

    async def enqueue(
        self, participant_id: Any, misses: list[dict[str, Any]],
        on_complete: Callable[[], Awaitable[None]],
        *, completion_key: Hashable | None = None,
    ) -> None:
        if (
            self._closing or not self.client or not misses
            or time.monotonic() < self._circuit_until
        ):
            return
        callback_key = completion_key or (
            str(participant_id),
            tuple(sorted(miss["fingerprint"] for miss in misses)),
        )
        tasks: set[asyncio.Task] = set()
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
                tasks.add(task)
            if not tasks:
                return
            state = self._completion_states.get(callback_key)
            if state is None:
                state = {"pending": set(), "on_complete": on_complete}
                completion = asyncio.create_task(
                    self._watch_completion(callback_key, state),
                    name="semantic-enrichment-completion",
                )
                state["watcher"] = completion
                self._completion_states[callback_key] = state
                self._completion_tasks.add(completion)
                completion.add_done_callback(self._completion_done)
            state["pending"].update(tasks)

    async def _watch_completion(
        self, callback_key: Hashable, state: dict[str, Any],
    ) -> None:
        any_success = False
        try:
            while True:
                async with self._inflight_lock:
                    pending = set(state["pending"])
                    state["pending"].clear()
                    if not pending:
                        if self._completion_states.get(callback_key) is state:
                            self._completion_states.pop(callback_key, None)
                        break
                results = await asyncio.gather(*pending, return_exceptions=True)
                any_success = any_success or any(result is True for result in results)
            if any_success:
                await state["on_complete"]()
        finally:
            async with self._inflight_lock:
                if self._completion_states.get(callback_key) is state:
                    self._completion_states.pop(callback_key, None)

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
            if time.monotonic() < self._circuit_until:
                return False
            try:
                cache_status, cached = await asyncio.to_thread(
                    self._validated_cached_external,
                    participant_id, miss["fingerprint"], miss["event"],
                )
                if cached is not None:
                    return True
                if cache_status == "rejected":
                    return False
                raw = await asyncio.to_thread(self.client.infer, miss["event"])
                status, external, rejection = _validate_response_components(
                    raw,
                    miss["event"],
                    model=self.model,
                )
                assessment = {"external": external}
                common = {
                    "schema_version": SEMANTIC_SCHEMA_VERSION,
                    "prompt_version": PROMPT_VERSION,
                    "model": self.model,
                }
                if status == "complete":
                    await asyncio.to_thread(
                        self.cache.put_complete,
                        participant_id,
                        miss["fingerprint"],
                        assessment,
                        **common,
                    )
                elif status == "partial":
                    await asyncio.to_thread(
                        self.cache.put_partial,
                        participant_id,
                        miss["fingerprint"],
                        assessment,
                        **common,
                    )
                else:
                    rejection = rejection or {
                        "reason": "all_components_rejected"
                    }
                    await asyncio.to_thread(
                        self.cache.put_rejected,
                        participant_id,
                        miss["fingerprint"],
                        reason=str(rejection["reason"]),
                        confidence=rejection.get("confidence"),
                        assessment=assessment,
                        **common,
                    )
                return status != "rejected"
            except SemanticContentRejected as exc:
                await asyncio.to_thread(
                    self.cache.put_rejected,
                    participant_id,
                    miss["fingerprint"],
                    reason=exc.reason,
                    confidence=exc.confidence,
                    schema_version=SEMANTIC_SCHEMA_VERSION,
                    prompt_version=PROMPT_VERSION,
                    model=self.model,
                )
                logger.info(
                    "semantic_enrichment_rejected fingerprint_prefix=%s "
                    "reason=%s confidence=%s",
                    str(miss.get("fingerprint") or "")[:12],
                    exc.reason,
                    exc.confidence,
                )
                return False
            except (SemanticProviderError, requests.RequestException) as exc:
                self._circuit_until = time.monotonic() + self._circuit_seconds
                provider = str(getattr(self.client, "provider", "unknown"))[:48]
                model = str(getattr(self.client, "model", self.model))[:80]
                message = _safe_error_message(exc)
                logger.warning(
                    "semantic_enrichment_failed fingerprint_prefix=%s provider=%s "
                    "model=%s error_class=%s message=%s circuit_open_seconds=%s",
                    str(miss.get("fingerprint") or "")[:12], provider, model,
                    type(exc).__name__, message, self._circuit_seconds,
                )
                return False
            except Exception as exc:
                provider = str(getattr(self.client, "provider", "unknown"))[:48]
                model = str(getattr(self.client, "model", self.model))[:80]
                logger.warning(
                    "semantic_enrichment_unclassified_failure "
                    "fingerprint_prefix=%s provider=%s model=%s "
                    "error_class=%s message=%s circuit_open_seconds=0",
                    str(miss.get("fingerprint") or "")[:12],
                    provider,
                    model,
                    type(exc).__name__,
                    _safe_error_message(exc),
                )
                return False
