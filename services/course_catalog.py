"""Deterministic in-memory retrieval over the generated course catalog."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
import hashlib
import json
import re
import unicodedata
from typing import Any, Mapping

from entry.class_info_data import CLASS_INFO_DICT
from entry.course_aliases import COURSE_ALIASES


COURSE_RESOLVER_VERSION = "course_catalog_resolver.v1"
_PUNCTUATION = re.compile(r"[\s·•,，。.!！?？:：;；_—–－()（）\[\]【】{}《》<>]+")
_COURSE_SUFFIX = re.compile(r"(?:课程|上课|课堂|课)$", flags=re.IGNORECASE)


def normalize_course_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold().strip()
    text = text.replace("Ⅰ", "i").replace("Ⅱ", "ii").replace("Ⅲ", "iii")
    return _PUNCTUATION.sub("", text)


def _catalog_revision(catalog: Mapping[str, Mapping[str, Any]]) -> str:
    payload = [
        {
            "canonical_name": name,
            "code": str(values.get("code") or ""),
            "credits": values.get("credits"),
            "hours": values.get("hours"),
            "hours_per_week": values.get("hours_per_week"),
        }
        for name, values in sorted(catalog.items())
    ]
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


COURSE_CATALOG_REVISION = _catalog_revision(CLASS_INFO_DICT)


@dataclass(frozen=True)
class CourseCandidate:
    canonical_name: str
    code: str
    credits: float | None
    hours: float | None
    hours_per_week: float | None
    local_score: float
    match_reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CourseResolution:
    query: str
    normalized_query: str
    expanded_query: str
    alias: str | None
    candidates: tuple[CourseCandidate, ...]
    exact_match: CourseCandidate | None
    catalog_revision: str
    resolver_version: str = COURSE_RESOLVER_VERSION

    @property
    def likely_course(self) -> bool:
        return bool(self.candidates and self.candidates[0].local_score >= 0.62)

    def context(self) -> dict[str, Any]:
        return {
            "catalog_revision": self.catalog_revision,
            "resolver_version": self.resolver_version,
            "query": self.query,
            "normalized_query": self.normalized_query,
            "expanded_query": self.expanded_query,
            "alias": self.alias,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
        }


class CourseCatalogResolver:
    def __init__(
        self,
        catalog: Mapping[str, Mapping[str, Any]] | None = None,
        *,
        aliases: Mapping[str, str] | None = None,
        max_candidates: int = 5,
        catalog_revision: str | None = None,
    ):
        self.catalog = dict(CLASS_INFO_DICT if catalog is None else catalog)
        self.aliases = dict(COURSE_ALIASES if aliases is None else aliases)
        self.max_candidates = max(1, min(int(max_candidates), 8))
        self.catalog_revision = catalog_revision or _catalog_revision(self.catalog)
        self._entries = tuple(
            (
                name,
                normalize_course_text(name),
                str(values.get("code") or "").casefold(),
                dict(values),
            )
            for name, values in self.catalog.items()
        )
        self._normalized_aliases = tuple(
            sorted(
                (
                    (
                        normalize_course_text(alias),
                        normalize_course_text(expansion),
                        alias,
                    )
                    for alias, expansion in self.aliases.items()
                ),
                key=lambda item: len(item[0]),
                reverse=True,
            )
        )

    def resolve(self, query: Any) -> CourseResolution:
        raw_query = str(query or "").strip()[:300]
        normalized = normalize_course_text(raw_query)
        course_query = _COURSE_SUFFIX.sub("", normalized)
        alias_key = None
        expanded = course_query
        alias_suffix = ""
        for normalized_alias, expansion, original_alias in self._normalized_aliases:
            position = course_query.find(normalized_alias)
            if position < 0:
                continue
            alias_key = original_alias
            alias_suffix = course_query[position + len(normalized_alias) :]
            expanded = course_query[:position] + expansion + alias_suffix
            break

        scored: list[tuple[float, str, CourseCandidate]] = []
        exact_match: CourseCandidate | None = None
        for canonical_name, canonical_normalized, code, metadata in self._entries:
            score, reasons = self._score(
                course_query,
                expanded,
                canonical_normalized,
                code,
                alias_key=alias_key,
                alias_suffix=alias_suffix,
            )
            if score < 0.42:
                continue
            candidate = CourseCandidate(
                canonical_name=canonical_name,
                code=str(metadata.get("code") or ""),
                credits=_optional_float(metadata.get("credits")),
                hours=_optional_float(metadata.get("hours")),
                hours_per_week=_optional_float(metadata.get("hours_per_week")),
                local_score=round(min(score, 1.0), 6),
                match_reasons=tuple(reasons),
            )
            scored.append((score, canonical_name, candidate))

        scored.sort(key=lambda item: (-item[0], len(item[1]), item[1]))
        candidates = tuple(item[2] for item in scored[: self.max_candidates])
        if exact_match is None and candidates:
            top = candidates[0]
            if top.local_score >= 0.97 and (
                normalize_course_text(top.canonical_name) == expanded
                or normalize_course_text(top.canonical_name) == course_query
            ):
                exact_match = top
        return CourseResolution(
            query=raw_query,
            normalized_query=course_query,
            expanded_query=expanded,
            alias=alias_key,
            candidates=candidates,
            exact_match=exact_match,
            catalog_revision=self.catalog_revision,
        )

    @staticmethod
    def _score(
        query: str,
        expanded: str,
        canonical: str,
        code: str,
        *,
        alias_key: str | None,
        alias_suffix: str,
    ) -> tuple[float, list[str]]:
        if not query:
            return 0.0, []
        reasons: list[str] = []
        if code and code in query.casefold():
            return 1.0, ["course_code_exact"]
        if query == canonical:
            return 1.0, ["canonical_exact"]
        if expanded == canonical:
            return 0.99, [f"alias:{alias_key}", "expanded_exact"]

        score = 0.0
        if expanded and canonical.startswith(expanded):
            score = 0.90
            reasons.append("expanded_prefix")
        elif expanded and expanded in canonical:
            score = 0.84
            reasons.append("expanded_containment")
        elif canonical in expanded:
            score = 0.80
            reasons.append("canonical_containment")

        similarity = SequenceMatcher(None, expanded or query, canonical).ratio()
        if similarity >= 0.45:
            score = max(score, 0.35 + 0.55 * similarity)
            reasons.append("sequence_similarity")
        overlap = _character_overlap(expanded or query, canonical)
        if overlap >= 0.45:
            score = max(score, 0.35 + 0.50 * overlap)
            reasons.append("character_overlap")

        suffix = alias_suffix.casefold()
        if suffix in {"a", "a类"} and "a类" in canonical:
            score += 0.09
            reasons.append("alias_variant:a")
        elif suffix in {"b", "b类"} and "b类" in canonical:
            score += 0.09
            reasons.append("alias_variant:b")
        if alias_key:
            reasons.insert(0, f"alias:{alias_key}")
        return score, list(dict.fromkeys(reasons))


def _character_overlap(left: str, right: str) -> float:
    left_chars = set(left)
    right_chars = set(right)
    if not left_chars or not right_chars:
        return 0.0
    return len(left_chars & right_chars) / len(left_chars)


def _optional_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


DEFAULT_COURSE_CATALOG_RESOLVER = CourseCatalogResolver()
