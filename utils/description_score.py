"""Lightweight deterministic appraisal rules used by the state model.

This module deliberately has no network, database, or model dependencies.  An
external semantic model is an upstream enrichment step; prediction only reads
the metadata already attached to an event.
"""

from __future__ import annotations

from settings.description_keywords import (
    DEFAULT_DESCRIPTION_SCORE,
    HARDCORE_WORDS,
    NEGATIVE_WORDS,
    POSITIVE_WORDS,
    RELAXING_WORDS,
    TIER1_WORDS,
    TIER2_WORDS,
    TIER3_WORDS,
)


def _contains_any(text: str, words: object) -> bool:
    return any(str(word).casefold() in text for word in words)


def score_description(description: str, summary: str = "") -> float:
    """Return a reproducible 1-10 appraisal prior from explicit wording.

    Objective difficulty in a title is intentionally kept separate from user
    appraisal.  With no subjective description, even a difficult course stays
    close to neutral instead of being interpreted as dislike.
    """

    desc = str(description or "").strip().casefold()
    title = str(summary or "").strip().casefold()
    full_text = f"{title} {desc}".strip()
    if not full_text:
        return float(DEFAULT_DESCRIPTION_SCORE)

    score = float(DEFAULT_DESCRIPTION_SCORE)
    if _contains_any(title, RELAXING_WORDS):
        score = max(score, 6.5)
    elif _contains_any(title, HARDCORE_WORDS):
        # Difficulty belongs to objective semantics.  A title alone is not
        # evidence of the participant's appraisal.
        score = 5.0

    if not desc:
        return round(max(1.0, min(10.0, score)), 3)

    has_positive = _contains_any(desc, POSITIVE_WORDS)
    has_negative = _contains_any(desc, NEGATIVE_WORDS)
    if has_positive:
        score += 1.5
    if has_negative:
        score -= 1.5

    tier = 0
    if _contains_any(full_text, TIER1_WORDS):
        tier = 1
    elif _contains_any(full_text, TIER2_WORDS):
        tier = 2
    elif _contains_any(full_text, TIER3_WORDS):
        tier = 3

    if tier and has_negative and not has_positive:
        score -= {1: 1.5, 2: 1.0, 3: 0.5}[tier]
    elif tier and has_positive and not has_negative:
        score += {1: 0.5, 2: 0.75, 3: 1.0}[tier]

    return round(max(1.0, min(10.0, score)), 3)


def convert_score_to_Flike(score: float) -> float:
    """Map a 1-10 appraisal score to the model's [-1, 1] preference factor."""

    bounded = max(1.0, min(10.0, float(score)))
    return round((bounded - 5.0) / 5.0, 3)
