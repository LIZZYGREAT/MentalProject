"""Validated, compact context returned by the generic Vision adapter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


class GenericImageContextValidationError(ValueError):
    pass


GENERIC_IMAGE_KINDS = frozenset(
    {
        "course_schedule",
        "calendar_screenshot",
        "code_or_error_screenshot",
        "document",
        "chart",
        "photo",
        "other",
    }
)

GENERIC_IMAGE_INTERACTION_HINTS = frozenset(
    {
        "question",
        "course_import_request",
        "calendar_event_request",
        "describe_only",
        "unknown",
    }
)


@dataclass(frozen=True)
class GenericImageContext:
    image_kind: Literal[
        "course_schedule",
        "calendar_screenshot",
        "code_or_error_screenshot",
        "document",
        "chart",
        "photo",
        "other",
    ]
    summary: str
    visible_text: str = ""
    warnings: tuple[str, ...] = ()
    interaction_hint: Literal[
        "question",
        "course_import_request",
        "calendar_event_request",
        "describe_only",
        "unknown",
    ] = "unknown"

    def __post_init__(self) -> None:
        if self.image_kind not in GENERIC_IMAGE_KINDS:
            raise GenericImageContextValidationError("unsupported image_kind")
        if self.interaction_hint not in GENERIC_IMAGE_INTERACTION_HINTS:
            raise GenericImageContextValidationError("unsupported interaction_hint")

    @classmethod
    def from_dict(cls, value: Any) -> "GenericImageContext":
        if not isinstance(value, dict):
            raise GenericImageContextValidationError("result must be an object")
        allowed = {
            "image_kind",
            "summary",
            "visible_text",
            "warnings",
            "interaction_hint",
        }
        required = allowed - {"interaction_hint"}
        if set(value) not in {frozenset(required), frozenset(allowed)}:
            raise GenericImageContextValidationError("unexpected result fields")
        image_kind = str(value.get("image_kind") or "").strip()
        summary = str(value.get("summary") or "").strip()
        visible_text = str(value.get("visible_text") or "").strip()
        warnings_value = value.get("warnings")
        interaction_hint = str(value.get("interaction_hint") or "unknown").strip().lower()
        if not image_kind or not summary or not isinstance(warnings_value, list):
            raise GenericImageContextValidationError("required field is invalid")
        if image_kind not in GENERIC_IMAGE_KINDS:
            raise GenericImageContextValidationError("unsupported image_kind")
        if interaction_hint not in GENERIC_IMAGE_INTERACTION_HINTS:
            raise GenericImageContextValidationError("unsupported interaction_hint")
        warnings = tuple(str(item).strip() for item in warnings_value if str(item).strip())
        return cls(
            image_kind=image_kind[:80],
            summary=summary[:4000],
            visible_text=visible_text[:8000],
            warnings=warnings[:20],
            interaction_hint=interaction_hint,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "image_kind": self.image_kind,
            "summary": self.summary,
            "visible_text": self.visible_text,
            "warnings": list(self.warnings),
            "interaction_hint": self.interaction_hint,
        }
