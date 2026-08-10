"""Reproducible event-semantics inference with bounded API assistance.

The external model is intentionally not asked to predict a person's stress.
It only extracts task semantics (difficulty, stakes, time pressure, and related
features).  Deterministic rules remain the anchor and explicit user appraisal
is applied later by the psychological state model.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from typing import Any, Dict, Mapping, Optional, Protocol

import requests

from services.event_semantic_prompt import (
    PROMPT_SHA256,
    PROMPT_VERSION,
    SEMANTIC_AGENT_SYSTEM_PROMPT,
)


SEMANTIC_SCHEMA_VERSION = "event_semantics.v2"
RULE_VERSION = "zh_event_rules.2026-08-01.v2"
FUSION_POLICY_VERSION = "rule_anchored_api_fusion.v2"

DIMENSIONS = (
    "difficulty",
    "cognitive_demand",
    "stakes",
    "time_pressure",
    "social_evaluation",
    "uncontrollability",
    "novelty",
    "expected_effort",
    "uncertainty",
    "unfinished",
)


def _clamp(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = float(default)
    if number > 10.0:
        number /= 100.0
    elif number > 1.0:
        number /= 10.0
    return max(0.0, min(1.0, number))


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


@dataclass(frozen=True)
class SemanticAssessment:
    schema_version: str
    rule_version: str
    prompt_version: str
    fusion_policy_version: str
    fingerprint: str
    source: str
    values: Dict[str, float]
    rule_values: Dict[str, float]
    external_values: Optional[Dict[str, float]]
    confidence: float
    evidence_tags: list[str]
    reasoning_summary: str
    matched_rules: list[str]
    constraints_applied: list[str]
    prompt_sha256: str = PROMPT_SHA256
    provider: Optional[str] = None
    model: Optional[str] = None
    cache_hit: bool = False
    external_error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class SemanticApiClient(Protocol):
    provider: str
    model: str

    def infer(self, payload: Mapping[str, Any]) -> Mapping[str, Any]: ...


BASE_PROFILES: Dict[str, Dict[str, float]] = {
    "course": {
        "difficulty": 0.58,
        "cognitive_demand": 0.66,
        "stakes": 0.34,
        "time_pressure": 0.18,
        "social_evaluation": 0.34,
        "uncontrollability": 0.35,
        "novelty": 0.24,
        "expected_effort": 0.64,
        "uncertainty": 0.32,
        "unfinished": 0.12,
    },
    "library": {
        "difficulty": 0.60,
        "cognitive_demand": 0.72,
        "stakes": 0.28,
        "time_pressure": 0.24,
        "social_evaluation": 0.05,
        "uncontrollability": 0.20,
        "novelty": 0.16,
        "expected_effort": 0.68,
        "uncertainty": 0.25,
        "unfinished": 0.25,
    },
    "task": {
        "difficulty": 0.45,
        "cognitive_demand": 0.50,
        "stakes": 0.30,
        "time_pressure": 0.28,
        "social_evaluation": 0.18,
        "uncontrollability": 0.25,
        "novelty": 0.30,
        "expected_effort": 0.52,
        "uncertainty": 0.32,
        "unfinished": 0.22,
    },
    "recovery": {
        "difficulty": 0.05,
        "cognitive_demand": 0.03,
        "stakes": 0.02,
        "time_pressure": 0.02,
        "social_evaluation": 0.02,
        "uncontrollability": 0.04,
        "novelty": 0.03,
        "expected_effort": 0.04,
        "uncertainty": 0.03,
        "unfinished": 0.0,
    },
}

TASK_PROFILES: Dict[str, Dict[str, float]] = {
    "exam": {
        "difficulty": 0.84,
        "cognitive_demand": 0.86,
        "stakes": 0.90,
        "time_pressure": 0.88,
        "social_evaluation": 0.82,
        "uncontrollability": 0.65,
        "novelty": 0.35,
        "expected_effort": 0.90,
        "uncertainty": 0.62,
        "unfinished": 0.20,
    },
    "ddl": {
        "difficulty": 0.76,
        "cognitive_demand": 0.75,
        "stakes": 0.78,
        "time_pressure": 0.92,
        "social_evaluation": 0.45,
        "uncontrollability": 0.57,
        "novelty": 0.30,
        "expected_effort": 0.86,
        "uncertainty": 0.52,
        "unfinished": 0.78,
    },
    "meeting": {
        "difficulty": 0.45,
        "cognitive_demand": 0.54,
        "stakes": 0.48,
        "time_pressure": 0.35,
        "social_evaluation": 0.64,
        "uncontrollability": 0.36,
        "novelty": 0.25,
        "expected_effort": 0.52,
        "uncertainty": 0.35,
        "unfinished": 0.12,
    },
    "homework": {
        "difficulty": 0.61,
        "cognitive_demand": 0.68,
        "stakes": 0.40,
        "time_pressure": 0.52,
        "social_evaluation": 0.15,
        "uncontrollability": 0.30,
        "novelty": 0.28,
        "expected_effort": 0.70,
        "uncertainty": 0.34,
        "unfinished": 0.46,
    },
}


# Each rule supplies lower bounds, not absolute truth.  This protects obvious
# domain facts (e.g. 数竞 is cognitively demanding) while leaving appraisal to
# the user and the dynamic model.
LEXICAL_RULES = (
    (
        "math_competition",
        r"数竞|数学竞赛|奥数|奥赛|竞赛题|math\s*competition|acm|icpc|codeforces",
        {
            "difficulty": 0.88,
            "cognitive_demand": 0.92,
            "stakes": 0.82,
            "time_pressure": 0.70,
            "social_evaluation": 0.68,
            "expected_effort": 0.90,
            "uncertainty": 0.60,
        },
    ),
    (
        "competition",
        r"竞赛|比赛|决赛|半决赛|锦标赛|competition|contest|tournament",
        {
            "difficulty": 0.80,
            "cognitive_demand": 0.76,
            "stakes": 0.84,
            "time_pressure": 0.72,
            "social_evaluation": 0.76,
            "expected_effort": 0.84,
        },
    ),
    (
        "algorithmic_work",
        r"算法|数据结构|动态规划|图论|leetcode|编程题|代码题|debug|调试",
        {
            "difficulty": 0.82,
            "cognitive_demand": 0.88,
            "stakes": 0.50,
            "time_pressure": 0.36,
            "uncontrollability": 0.34,
            "expected_effort": 0.80,
            "uncertainty": 0.46,
            "unfinished": 0.38,
        },
    ),
    (
        "advanced_math",
        r"离散数学|高等数学|高数|线性代数|线代|概率论|数学分析|抽象代数|实变函数|泛函分析",
        {
            "difficulty": 0.76,
            "cognitive_demand": 0.84,
            "expected_effort": 0.75,
            "uncertainty": 0.40,
        },
    ),
    (
        "exam_evaluation",
        r"考试|测验|期末|期中|面试|答辩|考核|quiz|exam|interview|defen[cs]e",
        {
            "difficulty": 0.76,
            "stakes": 0.88,
            "time_pressure": 0.84,
            "social_evaluation": 0.84,
            "uncontrollability": 0.62,
            "expected_effort": 0.84,
        },
    ),
    (
        "deadline_urgency",
        r"ddl|deadline|截止|提交|赶工|冲刺|临时抱佛脚|熬夜|通宵|火急|紧急",
        {
            "difficulty": 0.70,
            "stakes": 0.75,
            "time_pressure": 0.92,
            "uncontrollability": 0.72,
            "expected_effort": 0.88,
            "uncertainty": 0.58,
            "unfinished": 0.78,
        },
    ),
    (
        "project_research",
        r"项目|课题|研究|论文|报告|大作业|实验|建模|project|research|paper|report",
        {
            "difficulty": 0.66,
            "cognitive_demand": 0.72,
            "stakes": 0.46,
            "expected_effort": 0.72,
            "uncertainty": 0.44,
            "unfinished": 0.58,
        },
    ),
    (
        "difficulty_language",
        r"困难|很难|不会|陌生|卡住|复杂|棘手|hard|difficult|stuck|complex",
        {
            "difficulty": 0.78,
            "uncontrollability": 0.62,
            "expected_effort": 0.78,
            "uncertainty": 0.65,
        },
    ),
)


def infer_rule_semantics(
    *,
    name: str,
    description: str,
    event_type: str,
    task_type: str,
    duration_minutes: float,
) -> tuple[Dict[str, float], list[str], Dict[str, float]]:
    normalized_type = str(event_type or "task").lower()
    normalized_task = str(task_type or "general").lower()
    if normalized_type in {"rest", "meal", "nap", "sleep", "gym"}:
        base = dict(BASE_PROFILES["recovery"])
    else:
        base = dict(BASE_PROFILES.get(normalized_type, BASE_PROFILES["task"]))
    if normalized_type == "task" and normalized_task in TASK_PROFILES:
        base.update(TASK_PROFILES[normalized_task])

    text = f"{name or ''} {description or ''}".strip().lower()
    matched: list[str] = []
    floors: Dict[str, float] = {}
    for label, pattern, updates in LEXICAL_RULES:
        if re.search(pattern, text, flags=re.IGNORECASE):
            matched.append(label)
            for key, value in updates.items():
                base[key] = max(base.get(key, 0.0), value)
                floors[key] = max(floors.get(key, 0.0), value)

    duration = max(0.0, float(duration_minutes or 0.0))
    if duration >= 180.0 and normalized_type not in {
        "rest",
        "meal",
        "nap",
        "sleep",
    }:
        matched.append("long_duration")
        duration_pressure = min(0.82, 0.48 + (duration - 180.0) / 900.0)
        base["expected_effort"] = max(base["expected_effort"], duration_pressure)
        base["unfinished"] = max(base["unfinished"], min(0.66, duration / 720.0))

    if re.search(r"熟悉|擅长|有把握|喜欢|轻松|从容|familiar|confident|easy", text):
        matched.append("mastery_context")
        base["uncontrollability"] = min(base["uncontrollability"], 0.24)
        base["uncertainty"] = min(base["uncertainty"], 0.24)
        # Objective difficulty is not erased by positive appraisal.

    values = {key: _clamp(base.get(key, 0.0)) for key in DIMENSIONS}
    return values, matched, floors


def validate_external_semantics(
    raw: Mapping[str, Any],
) -> tuple[Dict[str, float], float, list[str], str]:
    if not isinstance(raw, Mapping):
        raise ValueError("semantic API response must be an object")
    candidate = raw.get("values", raw)
    if not isinstance(candidate, Mapping):
        raise ValueError("semantic API values must be an object")
    missing = [key for key in DIMENSIONS if key not in candidate]
    if missing:
        raise ValueError(f"semantic API response missing: {','.join(missing)}")
    values: Dict[str, float] = {}
    for key in DIMENSIONS:
        try:
            value = float(candidate[key])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"semantic API {key} must be numeric") from exc
        if not 0.0 <= value <= 1.0:
            raise ValueError(f"semantic API {key} must be within [0,1]")
        values[key] = value
    try:
        confidence = float(raw.get("confidence", candidate.get("confidence", 0.0)))
    except (TypeError, ValueError) as exc:
        raise ValueError("semantic API confidence must be numeric") from exc
    if not 0.0 <= confidence <= 1.0:
        raise ValueError("semantic API confidence must be within [0,1]")
    if confidence < 0.55:
        raise ValueError("semantic API confidence below 0.55")
    raw_tags = raw.get("evidence_tags", [])
    if not isinstance(raw_tags, list):
        raise ValueError("semantic API evidence_tags must be an array")
    evidence_tags = [
        str(item).strip()[:48]
        for item in raw_tags[:6]
        if str(item).strip()
    ]
    reasoning_summary = str(raw.get("reasoning_summary") or "").strip()[:160]
    return values, confidence, evidence_tags, reasoning_summary


def fuse_rule_and_external(
    rule_values: Mapping[str, float],
    external_values: Mapping[str, float],
    confidence: float,
    hard_floors: Mapping[str, float],
) -> tuple[Dict[str, float], list[str]]:
    """Bound external influence while preserving obvious rule-based facts."""

    weight = min(0.30, 0.30 * _clamp(confidence))
    fused: Dict[str, float] = {}
    constraints: list[str] = [f"api_weight_cap={weight:.3f}", "dimension_delta_cap=0.12"]
    for key in DIMENSIONS:
        rule = _clamp(rule_values.get(key, 0.0))
        external = _clamp(external_values.get(key, rule))
        delta = max(-0.12, min(0.12, weight * (external - rule)))
        candidate = _clamp(rule + delta)
        floor = _clamp(hard_floors.get(key, 0.0))
        if candidate < floor:
            candidate = floor
            constraints.append(f"rule_floor:{key}")
        fused[key] = candidate
    return fused, sorted(set(constraints))


class OpenAICompatibleSemanticClient:
    """Adapter for DeepSeek/OpenAI-compatible JSON inference endpoints."""

    def __init__(
        self,
        url: str,
        api_key: str,
        model: str,
        timeout: float = 8.0,
        *,
        provider: str = "deepseek",
        thinking: bool = False,
        max_tokens: int = 900,
    ):
        self.url = str(url).strip()
        self.api_key = str(api_key).strip()
        self.model = str(model).strip()
        self.provider = str(provider or "openai_compatible").strip().lower()
        self.thinking = bool(thinking)
        self.max_tokens = max(300, min(4000, int(max_tokens)))
        self.fingerprint_config = {
            "response_format": "json_object",
            "temperature": 0,
            "thinking": self.thinking,
            "max_tokens": self.max_tokens,
        }
        self.timeout = max(1.0, min(30.0, float(timeout)))

    def infer(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        output_example = {
            **{key: 0.0 for key in DIMENSIONS},
            "confidence": 0.0,
            "evidence_tags": ["简短事实标签"],
            "reasoning_summary": "不超过80字的可审计依据",
        }
        user_prompt = {
            "task": "请分析 event，并严格只返回一个 JSON 对象。",
            "event": dict(payload),
            "required_json_shape": output_example,
        }
        body = {
            "model": self.model,
            "temperature": 0,
            "stream": False,
            "max_tokens": self.max_tokens,
            "messages": [
                {"role": "system", "content": SEMANTIC_AGENT_SYSTEM_PROMPT},
                {"role": "user", "content": _canonical_json(user_prompt)},
            ],
            # DeepSeek V4 JSON mode guarantees valid JSON.  The exact field
            # contract is still validated locally before any value is fused.
            "response_format": {"type": "json_object"},
        }
        if self.provider == "deepseek":
            body["thinking"] = {
                "type": "enabled" if self.thinking else "disabled"
            }
        response = requests.post(
            self.url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=self.timeout,
        )
        response.raise_for_status()
        data = response.json()
        if isinstance(data, Mapping) and all(key in data for key in DIMENSIONS):
            return data
        content = data.get("output_text") if isinstance(data, Mapping) else None
        if not content and isinstance(data, Mapping):
            choices = data.get("choices") or []
            if choices:
                content = (choices[0].get("message") or {}).get("content")
        if isinstance(content, list):
            content = "".join(
                str(item.get("text", "")) if isinstance(item, Mapping) else str(item)
                for item in content
            )
        if not isinstance(content, str):
            raise ValueError("semantic API response has no JSON content")
        if not content.strip():
            raise ValueError("semantic API returned empty JSON content")
        return json.loads(content)


class SemanticInferenceCache:
    """Immutable fingerprint cache used to replay external semantic outputs."""

    def __init__(self, path: str):
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self.path), timeout=5.0)
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS semantic_inference_cache (
                fingerprint TEXT PRIMARY KEY,
                request_json TEXT NOT NULL,
                external_json TEXT NOT NULL,
                assessment_json TEXT NOT NULL,
                provider TEXT,
                model TEXT,
                prompt_version TEXT NOT NULL,
                policy_version TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def get(self, fingerprint: str) -> Optional[Dict[str, Any]]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT assessment_json FROM semantic_inference_cache WHERE fingerprint = ?",
                (fingerprint,),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def put(
        self,
        fingerprint: str,
        request_payload: Mapping[str, Any],
        external_payload: Mapping[str, Any],
        assessment: Mapping[str, Any],
        provider: str,
        model: str,
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO semantic_inference_cache(
                    fingerprint, request_json, external_json, assessment_json,
                    provider, model, prompt_version, policy_version, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fingerprint,
                    _canonical_json(request_payload),
                    _canonical_json(external_payload),
                    _canonical_json(assessment),
                    provider,
                    model,
                    PROMPT_VERSION,
                    FUSION_POLICY_VERSION,
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                ),
            )


class EventSemanticEngine:
    def __init__(
        self,
        api_client: Optional[SemanticApiClient] = None,
        cache: Optional[SemanticInferenceCache] = None,
    ):
        self.api_client = api_client
        self.cache = cache
        self._api_retry_after = 0.0
        self._last_api_error: Optional[str] = None

    def assess(
        self,
        *,
        name: str,
        description: str,
        event_type: str,
        task_type: str,
        duration_minutes: float,
        context: Optional[Mapping[str, Any]] = None,
        supplied_external: Optional[Mapping[str, Any]] = None,
        allow_external: bool = True,
    ) -> SemanticAssessment:
        raw_context = context if isinstance(context, Mapping) else {}
        raw_unfinished_names = raw_context.get("unfinished_task_names") or []
        if not isinstance(raw_unfinished_names, (list, tuple)):
            raw_unfinished_names = []
        raw_high_load_names = raw_context.get(
            "previous_day_high_load_event_names"
        ) or []
        if not isinstance(raw_high_load_names, (list, tuple)):
            raw_high_load_names = []
        safe_context = {
            "source_date": str(raw_context.get("source_date") or "")[:10],
            "previous_day_end_stress_band": str(
                raw_context.get("previous_day_end_stress_band") or ""
            )[:24],
            "unfinished_task_count": max(
                0,
                min(20, int(raw_context.get("unfinished_task_count") or 0)),
            ),
            "unfinished_task_names": [
                str(item).strip()[:80]
                for item in raw_unfinished_names[:5]
                if str(item).strip()
            ],
            "previous_day_high_load_event_names": [
                str(item).strip()[:80]
                for item in raw_high_load_names[:5]
                if str(item).strip()
            ],
            "explicit_unfinished": bool(raw_context.get("explicit_unfinished", False)),
        }
        safe_payload = {
            "name": str(name or "")[:160],
            "description": str(description or "")[:500],
            "event_type": str(event_type or "task").lower(),
            "task_type": str(task_type or "general").lower(),
            "duration_minutes": round(max(0.0, float(duration_minutes or 0.0)), 2),
            "schema_version": SEMANTIC_SCHEMA_VERSION,
            "prompt_version": PROMPT_VERSION,
            "prompt_sha256": PROMPT_SHA256,
            "context": safe_context,
        }
        rule_values, matched, floors = infer_rule_semantics(**{
            key: safe_payload[key]
            for key in ("name", "description", "event_type", "task_type", "duration_minutes")
        })
        api_eligible = bool(allow_external) and safe_payload["event_type"] not in {
            "rest",
            "meal",
            "nap",
            "sleep",
            "gym",
        }
        provider = getattr(self.api_client, "provider", None) if api_eligible else None
        model = getattr(self.api_client, "model", None) if api_eligible else None
        fingerprint_payload = {
            **safe_payload,
            "rule_version": RULE_VERSION,
            "fusion_policy_version": FUSION_POLICY_VERSION,
            "prompt_sha256": PROMPT_SHA256,
            "provider": provider,
            "model": model,
            "provider_options": getattr(
                self.api_client,
                "fingerprint_config",
                {},
            ) if api_eligible else {},
        }
        fingerprint = hashlib.sha256(
            _canonical_json(fingerprint_payload).encode("utf-8")
        ).hexdigest()

        if supplied_external is None and api_eligible and self.api_client and self.cache:
            cached = self.cache.get(fingerprint)
            if cached:
                cached["source"] = "api_cache"
                cached["cache_hit"] = True
                return SemanticAssessment(**cached)

        external_raw: Optional[Mapping[str, Any]] = supplied_external
        source = "rules"
        external_error = None
        if external_raw is None and api_eligible and self.api_client:
            if time.monotonic() < self._api_retry_after:
                external_error = self._last_api_error or "semantic API circuit open"
                source = "rules_api_circuit_fallback"
            else:
                try:
                    external_raw = self.api_client.infer(safe_payload)
                    source = "api_fused"
                    self._last_api_error = None
                except Exception as exc:  # network/model failures must not break simulation
                    external_error = f"{type(exc).__name__}: {str(exc)[:180]}"
                    self._last_api_error = external_error
                    self._api_retry_after = time.monotonic() + 60.0
                    source = "rules_api_fallback"
        elif external_raw is not None:
            source = "provided_external_fused"

        external_values = None
        evidence_tags: list[str] = []
        reasoning_summary = ""
        constraints: list[str] = []
        confidence = 0.82 if matched else 0.68
        values = dict(rule_values)
        if external_raw is not None:
            try:
                (
                    external_values,
                    external_confidence,
                    evidence_tags,
                    reasoning_summary,
                ) = validate_external_semantics(external_raw)
                values, constraints = fuse_rule_and_external(
                    rule_values,
                    external_values,
                    external_confidence,
                    floors,
                )
                confidence = max(confidence, external_confidence * 0.85)
            except (TypeError, ValueError) as exc:
                external_error = str(exc)
                source = "rules_invalid_api_fallback"
                external_values = None

        assessment = SemanticAssessment(
            schema_version=SEMANTIC_SCHEMA_VERSION,
            rule_version=RULE_VERSION,
            prompt_version=PROMPT_VERSION,
            fusion_policy_version=FUSION_POLICY_VERSION,
            fingerprint=fingerprint,
            source=source,
            values={key: round(value, 6) for key, value in values.items()},
            rule_values={key: round(value, 6) for key, value in rule_values.items()},
            external_values=(
                {key: round(value, 6) for key, value in external_values.items()}
                if external_values
                else None
            ),
            confidence=round(_clamp(confidence), 6),
            evidence_tags=evidence_tags,
            reasoning_summary=reasoning_summary,
            matched_rules=matched,
            constraints_applied=constraints,
            prompt_sha256=PROMPT_SHA256,
            provider=provider,
            model=model,
            cache_hit=False,
            external_error=external_error,
        )
        if (
            supplied_external is None
            and api_eligible
            and self.api_client
            and self.cache
            and external_values is not None
        ):
            self.cache.put(
                fingerprint,
                safe_payload,
                external_raw or {},
                assessment.to_dict(),
                provider or "unknown",
                model or "unknown",
            )
        return assessment


_DEFAULT_ENGINE: Optional[EventSemanticEngine] = None


def default_semantic_engine() -> EventSemanticEngine:
    global _DEFAULT_ENGINE
    if _DEFAULT_ENGINE is not None:
        return _DEFAULT_ENGINE
    enabled = os.getenv("SEMANTIC_API_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    provider = os.getenv("SEMANTIC_API_PROVIDER", "deepseek").strip().lower()
    url = os.getenv(
        "SEMANTIC_API_URL",
        "https://api.deepseek.com/chat/completions",
    ).strip()
    api_key = (
        os.getenv("DEEPSEEK_API_KEY", "").strip()
        if provider == "deepseek"
        else os.getenv("SEMANTIC_API_KEY", "").strip()
    )
    model = os.getenv("SEMANTIC_API_MODEL", "deepseek-v4-flash").strip()
    client: Optional[SemanticApiClient] = None
    cache: Optional[SemanticInferenceCache] = None
    if enabled and url and api_key and model:
        client = OpenAICompatibleSemanticClient(
            url,
            api_key,
            model,
            timeout=float(os.getenv("SEMANTIC_API_TIMEOUT_SECONDS", "12")),
            provider=provider,
            thinking=os.getenv("SEMANTIC_API_THINKING", "false").strip().lower()
            in {"1", "true", "yes", "on", "enabled"},
            max_tokens=int(os.getenv("SEMANTIC_API_MAX_TOKENS", "900")),
        )
        cache = SemanticInferenceCache(
            os.getenv(
                "SEMANTIC_CACHE_PATH",
                str(Path("data") / "semantic_inference.sqlite3"),
            )
        )
    _DEFAULT_ENGINE = EventSemanticEngine(api_client=client, cache=cache)
    return _DEFAULT_ENGINE


def assess_event_semantics(
    *,
    name: str,
    description: str,
    event_type: str,
    task_type: str,
    duration_minutes: float,
    context: Optional[Mapping[str, Any]] = None,
    supplied_external: Optional[Mapping[str, Any]] = None,
    allow_external: bool = True,
) -> Dict[str, Any]:
    return default_semantic_engine().assess(
        name=name,
        description=description,
        event_type=event_type,
        task_type=task_type,
        duration_minutes=duration_minutes,
        context=context,
        supplied_external=supplied_external,
        allow_external=allow_external,
    ).to_dict()


def semantic_agent_status() -> Dict[str, Any]:
    """Return non-secret runtime readiness and version information."""

    enabled = os.getenv("SEMANTIC_API_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    provider = os.getenv("SEMANTIC_API_PROVIDER", "deepseek").strip().lower()
    url = os.getenv(
        "SEMANTIC_API_URL",
        "https://api.deepseek.com/chat/completions",
    ).strip()
    model = os.getenv("SEMANTIC_API_MODEL", "deepseek-v4-flash").strip()
    key_present = bool(
        os.getenv("DEEPSEEK_API_KEY", "").strip()
        if provider == "deepseek"
        else os.getenv("SEMANTIC_API_KEY", "").strip()
    )
    missing = []
    if not key_present:
        missing.append(
            "DEEPSEEK_API_KEY" if provider == "deepseek" else "SEMANTIC_API_KEY"
        )
    if not url:
        missing.append("SEMANTIC_API_URL")
    if not model:
        missing.append("SEMANTIC_API_MODEL")
    return {
        "enabled": enabled,
        "configured": bool(enabled and not missing),
        "mode": "agent_bounded_by_rules" if enabled and not missing else "deterministic_rules",
        "provider": provider,
        "model": model or None,
        "prompt_version": PROMPT_VERSION,
        "prompt_sha256": PROMPT_SHA256,
        "rule_version": RULE_VERSION,
        "fusion_policy_version": FUSION_POLICY_VERSION,
        "cache_path": os.getenv(
            "SEMANTIC_CACHE_PATH",
            str(Path("data") / "semantic_inference.sqlite3"),
        ),
        "missing": missing,
        "key_present": key_present,
    }


def reset_default_semantic_engine() -> None:
    """Allow tests or a controlled service reload to re-read environment config."""

    global _DEFAULT_ENGINE
    _DEFAULT_ENGINE = None
