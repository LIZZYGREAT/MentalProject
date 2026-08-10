"""Load SKILL.md as instructions only; never import or execute skill content."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class SkillSnapshot:
    metadata: dict[str, Any]
    instructions: str
    version: str


class SkillLoader:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._snapshot: SkillSnapshot | None = None

    def load(self) -> SkillSnapshot:
        text = self.path.read_text(encoding="utf-8")
        metadata: dict[str, Any] = {}
        instructions = text
        if text.startswith("---\n"):
            end = text.find("\n---\n", 4)
            if end < 0:
                raise ValueError("SKILL.md has unterminated YAML frontmatter")
            parsed = yaml.safe_load(text[4:end]) or {}
            if not isinstance(parsed, dict):
                raise ValueError("SKILL.md frontmatter must be a mapping")
            metadata = parsed
            instructions = text[end + 5 :].strip()
        if not instructions:
            raise ValueError("SKILL.md instructions are empty")
        version = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        self._snapshot = SkillSnapshot(metadata, instructions, version)
        return self._snapshot

    def current(self) -> SkillSnapshot:
        if self._snapshot is None:
            return self.load()
        return self._snapshot
