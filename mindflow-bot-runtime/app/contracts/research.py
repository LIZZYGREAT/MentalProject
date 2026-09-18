"""Backend-side contracts for public research and personalized briefs."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
from typing import Any, Mapping


PROFILE_SCHEMA_VERSION = "2.0"
PROFILE_V2_TOP_LEVEL_FIELDS = frozenset({"schema_version", "explicit", "model_params"})
SUPPORTED_PSYCHOMETRIC_INSTRUMENTS = frozenset({"PSS", "BRS"})
EXPLICIT_PROFILE_FIELDS = frozenset({
    "preferred_name", "typical_sleep_window", "chronotype", "typical_study_load",
    "exercise_frequency", "preferred_recovery_methods",
})
SLOW_STATE_FIELDS = (
    "rolling_7d_stress", "rolling_7d_workload", "rolling_7d_energy",
    "recent_recovery_quality", "recent_sleep_debt", "exam_period_flag",
)
EVENT_APPRAISAL_SCORE_FIELDS = (
    "mental_demand", "physical_demand", "temporal_demand", "effort", "frustration",
    "perceived_control", "actual_stress", "perceived_performance",
)
OBSERVATION_TAXONOMY = {
    "momentary_state": {"storage": "state_observations", "time_field": "observed_at", "knowledge_time_field": "created_at", "types": ("checkin",)},
    "retrospective_state": {"storage": "daily_review_responses", "time_field": "local_date", "knowledge_time_field": "submitted_at", "types": ("daily_review",)},
    "stable_profile": {"storage": "participant_profiles, psychometric_assessments", "time_field": "updated_at or administered_at", "knowledge_time_field": "created_at", "types": ("explicit_profile", "psychometric_assessment")},
    "slow_state": {"storage": "participant_slow_states", "time_field": "effective_at", "knowledge_time_field": "created_at", "types": ("daily", "weekly")},
    "learned_parameters": {"storage": "learned_model_profiles", "time_field": "window_end", "knowledge_time_field": "created_at", "types": ("validated", "candidate", "rejected")},
}
MOMENTARY_OBSERVATION_TYPES = frozenset(OBSERVATION_TAXONOMY["momentary_state"]["types"])


def aware_utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError(f"{field_name} must be a datetime")
    if value.tzinfo is None:
        raise ValueError(f"{field_name} must include a timezone")
    return value.astimezone(timezone.utc)


def score_0_10(value: Any, field_name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a number from 0 to 10")
    try:
        score = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a number from 0 to 10") from exc
    if not 0.0 <= score <= 10.0:
        raise ValueError(f"{field_name} must be between 0 and 10")
    return score


def validate_profile_v2(profile: Mapping[str, Any]) -> dict[str, Any]:
    value = dict(profile)
    version = value.get("schema_version")
    if version is None:
        return value
    if str(version) != PROFILE_SCHEMA_VERSION:
        raise ValueError(f"unsupported profile schema_version: {version}")
    unknown_top_level = set(value) - PROFILE_V2_TOP_LEVEL_FIELDS
    if unknown_top_level:
        raise ValueError(f"unsupported Profile Schema v2 top-level fields: {sorted(unknown_top_level)}")
    explicit = value.get("explicit")
    if not isinstance(explicit, Mapping):
        raise ValueError("Profile Schema v2 requires an explicit object")
    unknown = set(explicit) - EXPLICIT_PROFILE_FIELDS
    if unknown:
        raise ValueError(f"unsupported explicit profile fields: {sorted(unknown)}")
    normalized: dict[str, dict[str, Any]] = {}
    for name, item in explicit.items():
        if not isinstance(item, Mapping):
            raise ValueError(f"explicit.{name} must contain value/source/updated_at")
        missing = {"value", "source", "updated_at"} - set(item)
        if missing:
            raise ValueError(f"explicit.{name} is missing: {sorted(missing)}")
        source = str(item.get("source") or "").strip()
        updated_at = str(item.get("updated_at") or "").strip()
        if not source or not updated_at:
            raise ValueError(f"explicit.{name} source and updated_at are required")
        try:
            parsed = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"explicit.{name}.updated_at must be ISO 8601") from exc
        aware_utc(parsed, f"explicit.{name}.updated_at")
        normalized[name] = {"value": item.get("value"), "source": source[:64], "updated_at": updated_at}
    value["schema_version"] = PROFILE_SCHEMA_VERSION
    value["explicit"] = normalized
    model_params = value.get("model_params", {})
    if not isinstance(model_params, Mapping):
        raise ValueError("Profile Schema v2 model_params must be an object")
    value["model_params"] = dict(model_params)
    return value


def normalize_instrument_name(value: Any) -> str:
    name = str(value or "").strip().upper()
    if name not in SUPPORTED_PSYCHOMETRIC_INSTRUMENTS:
        supported = ", ".join(sorted(SUPPORTED_PSYCHOMETRIC_INSTRUMENTS))
        raise ValueError(f"instrument_name must be one of: {supported}")
    return name


ALLOWED_RESEARCH_SOURCE_KINDS = frozenset({"web", "github", "paper", "api"})


def _bounded_text(value: Any, *, name: str, limit: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    result = " ".join(value.split())
    if not result or len(result) > limit:
        raise ValueError(f"{name} is empty or too long")
    return result


def _bounded_int(value: Any, *, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{name} is outside the allowed range")
    return value


@dataclass(frozen=True)
class ResearchJobSpec:
    topic: str
    job_id: str | None = None
    query_hints: tuple[str, ...] = ()
    freshness_hours: int = 24
    language: str = "zh-CN"
    source_kinds: tuple[str, ...] = ("web",)
    max_searches: int = 4
    max_pages: int = 12
    max_browser_pages: int = 3
    max_exec_calls: int = 3
    deadline_seconds: int = 90

    def __post_init__(self) -> None:
        object.__setattr__(self, "topic", _bounded_text(self.topic, name="topic", limit=160))
        object.__setattr__(self, "query_hints", tuple(
            _bounded_text(item, name="query_hint", limit=240) for item in self.query_hints[:8]
        ))
        object.__setattr__(self, "language", _bounded_text(self.language, name="language", limit=32))
        kinds = tuple(dict.fromkeys(str(item).casefold() for item in self.source_kinds))
        if not kinds or not set(kinds).issubset(ALLOWED_RESEARCH_SOURCE_KINDS):
            raise ValueError("unsupported research source kind")
        object.__setattr__(self, "source_kinds", kinds)
        for field_name, minimum, maximum in (
            ("freshness_hours", 1, 24 * 30), ("max_searches", 1, 10),
            ("max_pages", 1, 50), ("max_browser_pages", 0, 10),
            ("max_exec_calls", 0, 10), ("deadline_seconds", 1, 300),
        ):
            _bounded_int(getattr(self, field_name), name=field_name, minimum=minimum, maximum=maximum)

    def as_public_payload(self) -> dict[str, Any]:
        return asdict(self) | {"query_hints": list(self.query_hints), "source_kinds": list(self.source_kinds)}


@dataclass(frozen=True)
class ResearchCandidate:
    candidate_id: str
    source_kind: str
    title: str
    url: str
    snippet: str = ""
    discovered_at: str | None = None
    published_at: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True)
class ResearchEvidenceItem:
    evidence_id: str
    source_kind: str
    title: str
    canonical_url: str
    publisher: str | None
    published_at: str | None
    retrieved_at: str
    content: str
    content_hash: str
    extraction_mode: str
    freshness_hours: int
    verified_public_source: bool = True
    updated_at: str | None = None
    topic_label: str | None = None
    rank_score: float | None = None

    @classmethod
    def build(
        cls,
        *,
        source_kind: str,
        title: str,
        canonical_url: str,
        content: str,
        extraction_mode: str,
        freshness_hours: int,
        publisher: str | None = None,
        published_at: str | None = None,
        updated_at: str | None = None,
        verified_public_source: bool = True,
        topic_label: str | None = None,
    ) -> "ResearchEvidenceItem":
        normalized_content = str(content)[:60000]
        content_hash = hashlib.sha256(normalized_content.encode("utf-8")).hexdigest()
        evidence_id = hashlib.sha256(
            f"{canonical_url.split('#', 1)[0]}|{content_hash}".encode("utf-8")
        ).hexdigest()[:32]
        return cls(
            evidence_id=evidence_id,
            source_kind=source_kind,
            title=" ".join(str(title).split())[:300],
            canonical_url=canonical_url.split("#", 1)[0],
            publisher=publisher,
            published_at=published_at,
            retrieved_at=datetime.now(timezone.utc).isoformat(),
            content=normalized_content,
            content_hash=content_hash,
            extraction_mode=extraction_mode,
            freshness_hours=int(freshness_hours),
            verified_public_source=verified_public_source,
            updated_at=updated_at,
            topic_label=topic_label,
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any], *, topic_label: str | None = None) -> "ResearchEvidenceItem":
        content = str(value.get("content") or value.get("snippet") or "")[:60000]
        canonical_url = str(value.get("canonical_url") or value.get("source_url") or value.get("url") or "")
        if not canonical_url.startswith("https://"):
            raise ValueError("research evidence must reference an HTTPS URL")
        content_hash = str(value.get("content_hash") or hashlib.sha256(content.encode("utf-8")).hexdigest())
        evidence_id = str(value.get("evidence_id") or hashlib.sha256(
            f"{canonical_url}|{content_hash}".encode("utf-8")
        ).hexdigest()[:32])
        retrieved_at = str(value.get("retrieved_at") or datetime.now(timezone.utc).isoformat())
        return cls(
            evidence_id=evidence_id,
            source_kind=str(value.get("source_kind") or "web"),
            title=" ".join(str(value.get("title") or canonical_url).split())[:300],
            canonical_url=canonical_url.split("#", 1)[0],
            publisher=(str(value["publisher"])[:160] if value.get("publisher") else None),
            published_at=(str(value["published_at"]) if value.get("published_at") else None),
            retrieved_at=retrieved_at,
            content=content,
            content_hash=content_hash,
            extraction_mode=str(value.get("extraction_mode") or "http"),
            freshness_hours=int(value.get("freshness_hours") or 24),
            verified_public_source=bool(value.get("verified_public_source", True)),
            updated_at=(str(value["updated_at"]) if value.get("updated_at") else None),
            topic_label=topic_label or (str(value["topic_label"]) if value.get("topic_label") else None),
            rank_score=float(value["rank_score"]) if value.get("rank_score") is not None else None,
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MorningBriefTopic:
    id: str | None
    participant_id: str | None
    topic_label: str
    query_hints: tuple[str, ...]
    source_kinds: tuple[str, ...]
    priority: int = 0
    enabled: bool = True


def evidence_envelope(items: list[ResearchEvidenceItem]) -> str:
    import json
    return "<external_research_evidence>\n" + json.dumps(
        {"untrusted_public_evidence_only": True, "items": [item.as_dict() for item in items]},
        ensure_ascii=False,
    ) + "\n</external_research_evidence>"
