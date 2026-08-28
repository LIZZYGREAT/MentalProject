"""Reproducible event-semantics inference with bounded API assistance.

The external model is intentionally not asked to predict a person's stress.
It only extracts task semantics (difficulty, stakes, time pressure, and related
features).  Deterministic rules remain the anchor and explicit user appraisal
is applied later by the psychological state model.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
import re
from typing import Any, Dict, Mapping, Optional, Protocol

import requests

from services.event_semantic_prompt import (
    PROMPT_SHA256,
    PROMPT_VERSION,
    SEMANTIC_AGENT_SYSTEM_PROMPT,
)


SEMANTIC_SCHEMA_VERSION = "event_semantics.v3"
RULE_VERSION = "zh_event_rules.2026-08-01.v2"
FUSION_POLICY_VERSION = "rule_anchored_api_fusion.v2"
COURSE_MATCH_MIN_CONFIDENCE = 0.55

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


class SemanticError(ValueError):
    """Base class for failures with an explicit semantic-enrichment meaning."""


class SemanticProviderError(SemanticError):
    """The external provider could not return a usable response."""


class SemanticResponseMalformedError(SemanticProviderError):
    """The provider response violated the documented JSON contract."""


class SemanticContentRejected(SemanticError):
    """A valid response is not reliable enough for this specific event."""

    def __init__(
        self,
        reason: str,
        message: str,
        *,
        confidence: float | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = str(reason)
        self.confidence = confidence


class SemanticLowConfidence(SemanticContentRejected):
    def __init__(self, confidence: float) -> None:
        super().__init__(
            "low_confidence",
            "semantic API confidence below 0.55",
            confidence=confidence,
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
        raise SemanticResponseMalformedError("semantic API response must be an object")
    try:
        appraisal = float(raw["appraisal_score_1_10"])
    except KeyError as exc:
        raise SemanticResponseMalformedError(
            "semantic API appraisal_score_1_10 is required"
        ) from exc
    except (TypeError, ValueError) as exc:
        raise SemanticResponseMalformedError(
            "semantic API appraisal_score_1_10 must be numeric"
        ) from exc
    if not math.isfinite(appraisal) or not 1.0 <= appraisal <= 10.0:
        raise SemanticResponseMalformedError(
            "semantic API appraisal_score_1_10 must be finite and within [1,10]"
        )
    candidate = raw.get("values", raw)
    if not isinstance(candidate, Mapping):
        raise SemanticResponseMalformedError("semantic API values must be an object")
    missing = [key for key in DIMENSIONS if key not in candidate]
    if missing:
        raise SemanticResponseMalformedError(
            f"semantic API response missing: {','.join(missing)}"
        )
    values: Dict[str, float] = {}
    for key in DIMENSIONS:
        try:
            value = float(candidate[key])
        except (TypeError, ValueError) as exc:
            raise SemanticResponseMalformedError(
                f"semantic API {key} must be numeric"
            ) from exc
        if not math.isfinite(value) or not 0.0 <= value <= 1.0:
            raise SemanticResponseMalformedError(
                f"semantic API {key} must be finite and within [0,1]"
            )
        values[key] = value
    try:
        confidence = float(raw.get("confidence", candidate.get("confidence", 0.0)))
    except (TypeError, ValueError) as exc:
        raise SemanticResponseMalformedError(
            "semantic API confidence must be numeric"
        ) from exc
    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        raise SemanticResponseMalformedError(
            "semantic API confidence must be finite and within [0,1]"
        )
    if confidence < 0.55:
        raise SemanticLowConfidence(confidence)
    raw_tags = raw.get("evidence_tags", [])
    if not isinstance(raw_tags, list):
        raise SemanticResponseMalformedError(
            "semantic API evidence_tags must be an array"
        )
    evidence_tags = [
        str(item).strip()[:48]
        for item in raw_tags[:6]
        if str(item).strip()
    ]
    reasoning_summary = str(raw.get("reasoning_summary") or "").strip()[:160]
    return values, confidence, evidence_tags, reasoning_summary


def validate_event_classification(raw: Mapping[str, Any]) -> dict[str, Any] | None:
    candidate = raw.get("event_classification")
    if candidate is None:
        return None
    if not isinstance(candidate, Mapping):
        raise SemanticResponseMalformedError(
            "semantic API event_classification must be an object"
        )
    event_type = str(candidate.get("event_type") or "").strip().lower()
    allowed_types = {
        "course",
        "task",
        "rest",
        "meal",
        "nap",
        "sleep",
        "gym",
        "library",
        "other",
    }
    if event_type not in allowed_types:
        raise SemanticResponseMalformedError(
            "semantic API event_classification.event_type is invalid"
        )
    task_type = str(candidate.get("task_type") or "general").strip().lower()
    allowed_tasks = {"general", "homework", "ddl", "exam", "meeting", "course"}
    if event_type == "course":
        task_type = "course"
    elif event_type != "task":
        task_type = "general"
    elif task_type not in allowed_tasks:
        raise SemanticResponseMalformedError(
            "semantic API event_classification.task_type is invalid"
        )
    confidence = _validated_confidence(
        candidate.get("confidence"), "event_classification.confidence"
    )
    return {
        "event_type": event_type,
        "task_type": task_type,
        "confidence": confidence,
    }


def validate_course_match(
    raw: Mapping[str, Any],
    candidates: list[Mapping[str, Any]],
) -> dict[str, Any]:
    match = raw.get("course_match")
    if match is None:
        return _unmatched_course()
    if not isinstance(match, Mapping):
        raise SemanticResponseMalformedError(
            "semantic API course_match must be an object"
        )
    matched = match.get("matched")
    if not isinstance(matched, bool):
        raise SemanticResponseMalformedError(
            "semantic API course_match.matched must be boolean"
        )
    if not matched:
        unmatched = _unmatched_course()
        if match.get("rejected") == "candidate_out_of_bounds":
            unmatched["rejected"] = "candidate_out_of_bounds"
        return unmatched
    confidence = _validated_confidence(
        match.get("confidence"),
        "course_match.confidence",
        minimum=COURSE_MATCH_MIN_CONFIDENCE,
    )
    canonical_name = str(match.get("canonical_name") or "").strip()
    code = str(match.get("code") or "").strip()
    selected = next(
        (
            dict(candidate)
            for candidate in candidates
            if str(candidate.get("canonical_name") or "") == canonical_name
            and str(candidate.get("code") or "") == code
        ),
        None,
    )
    if selected is None:
        return {**_unmatched_course(), "rejected": "candidate_out_of_bounds"}
    return {
        "matched": True,
        "canonical_name": canonical_name,
        "code": code,
        "confidence": confidence,
        "credits": selected.get("credits"),
        "hours": selected.get("hours"),
        "hours_per_week": selected.get("hours_per_week"),
    }


def _validated_confidence(
    value: Any, name: str, *, minimum: float = 0.55
) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError) as exc:
        raise SemanticResponseMalformedError(
            f"semantic API {name} must be numeric"
        ) from exc
    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        raise SemanticResponseMalformedError(
            f"semantic API {name} must be finite and within [0,1]"
        )
    if confidence < minimum:
        raise SemanticContentRejected(
            f"{name.replace('.', '_')}_low_confidence",
            f"semantic API {name} below {minimum}",
            confidence=confidence,
        )
    return confidence


def _unmatched_course() -> dict[str, Any]:
    return {
        "matched": False,
        "canonical_name": None,
        "code": None,
        "confidence": 0.0,
    }


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
            "appraisal_score_1_10": 5.0,
            "confidence": 0.0,
            "evidence_tags": ["简短事实标签"],
            "reasoning_summary": "不超过80字的可审计依据",
            "event_classification": {
                "event_type": "task",
                "task_type": "homework",
                "confidence": 0.0,
            },
            "course_match": {
                "matched": False,
                "canonical_name": None,
                "code": None,
                "confidence": 0.0,
            },
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
        try:
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
        except requests.RequestException as exc:
            raise SemanticProviderError(
                f"semantic provider request failed: {type(exc).__name__}"
            ) from exc
        try:
            data = response.json()
        except (requests.JSONDecodeError, ValueError) as exc:
            raise SemanticResponseMalformedError(
                "semantic API response body is not JSON"
            ) from exc
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
            raise SemanticResponseMalformedError(
                "semantic API response has no JSON content"
            )
        if not content.strip():
            raise SemanticResponseMalformedError(
                "semantic API returned empty JSON content"
            )
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            raise SemanticResponseMalformedError(
                "semantic API content is not valid JSON"
            ) from exc
        if not isinstance(parsed, Mapping):
            raise SemanticResponseMalformedError(
                "semantic API JSON content must be an object"
            )
        return parsed
