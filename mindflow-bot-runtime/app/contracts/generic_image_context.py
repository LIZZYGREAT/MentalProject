"""Validated, compact context returned by the generic Vision adapter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class GenericImageContextValidationError(ValueError):
    pass


@dataclass(frozen=True)
class GenericImageContext:
    image_kind: str
    summary: str
    visible_text: str
    warnings: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, value: Any) -> "GenericImageContext":
        if not isinstance(value, dict):
            raise GenericImageContextValidationError("result must be an object")
        allowed = {"image_kind", "summary", "visible_text", "warnings"}
        if set(value) != allowed:
            raise GenericImageContextValidationError("unexpected result fields")
        image_kind = str(value.get("image_kind") or "").strip()
        summary = str(value.get("summary") or "").strip()
        visible_text = str(value.get("visible_text") or "").strip()
        warnings_value = value.get("warnings")
        if not image_kind or not summary or not isinstance(warnings_value, list):
            raise GenericImageContextValidationError("required field is invalid")
        warnings = tuple(str(item).strip() for item in warnings_value if str(item).strip())
        return cls(
            image_kind=image_kind[:80],
            summary=summary[:4000],
            visible_text=visible_text[:8000],
            warnings=warnings[:20],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "image_kind": self.image_kind,
            "summary": self.summary,
            "visible_text": self.visible_text,
            "warnings": list(self.warnings),
        }
