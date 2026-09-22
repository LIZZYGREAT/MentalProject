"""Typed, dependency-light loaders for Stage 1 JSON and JSONL artifacts."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Iterable, Mapping


class ArtifactLoadError(ValueError):
    """Raised when an artifact cannot be decoded or has the wrong top-level type."""


@dataclass(frozen=True)
class Scenario:
    scenario_id: str
    scenario_version: str
    pack_id: str
    participant_id: str
    presentation_mode: str
    annotation_time: str
    known_at_cutoff: str
    payload: Mapping[str, Any]

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "Scenario":
        required = (
            "scenario_id",
            "scenario_version",
            "pack_id",
            "participant_id",
            "presentation_mode",
            "annotation_time",
            "known_at_cutoff",
        )
        missing = [name for name in required if name not in value]
        if missing:
            raise ArtifactLoadError(f"scenario is missing fields: {', '.join(missing)}")
        return cls(**{name: str(value[name]) for name in required}, payload=dict(value))


@dataclass(frozen=True)
class AnnotationDocument:
    scenario_id: str
    annotation_module: str
    annotator_id: str
    annotation_round: str
    manual_version: str
    records: tuple[Mapping[str, Any], ...]
    payload: Mapping[str, Any]

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "AnnotationDocument":
        required = (
            "scenario_id",
            "annotation_module",
            "annotator_id",
            "annotation_round",
            "manual_version",
            "records",
        )
        missing = [name for name in required if name not in value]
        if missing:
            raise ArtifactLoadError(f"annotation is missing fields: {', '.join(missing)}")
        records = value["records"]
        if not isinstance(records, list) or not all(isinstance(item, Mapping) for item in records):
            raise ArtifactLoadError("annotation records must be an array of objects")
        return cls(
            scenario_id=str(value["scenario_id"]),
            annotation_module=str(value["annotation_module"]),
            annotator_id=str(value["annotator_id"]),
            annotation_round=str(value["annotation_round"]),
            manual_version=str(value["manual_version"]),
            records=tuple(dict(item) for item in records),
            payload=dict(value),
        )


def load_json(path: str | Path) -> Any:
    artifact_path = Path(path)
    try:
        return json.loads(artifact_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ArtifactLoadError(f"cannot load {artifact_path}: {exc}") from exc


def load_jsonl(path: str | Path) -> list[dict[str, Any]]:
    artifact_path = Path(path)
    values: list[dict[str, Any]] = []
    try:
        with artifact_path.open("r", encoding="utf-8") as stream:
            for line_number, raw_line in enumerate(stream, start=1):
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ArtifactLoadError(
                        f"cannot load {artifact_path}:{line_number}: {exc.msg}"
                    ) from exc
                if not isinstance(value, dict):
                    raise ArtifactLoadError(
                        f"cannot load {artifact_path}:{line_number}: expected object"
                    )
                values.append(value)
    except OSError as exc:
        raise ArtifactLoadError(f"cannot load {artifact_path}: {exc}") from exc
    return values


def iter_artifacts(paths: Iterable[str | Path]) -> Iterable[tuple[Path, int, dict[str, Any]]]:
    for raw_path in paths:
        path = Path(raw_path)
        if path.suffix == ".jsonl":
            for index, value in enumerate(load_jsonl(path), start=1):
                yield path, index, value
        else:
            value = load_json(path)
            if not isinstance(value, dict):
                raise ArtifactLoadError(f"cannot load {path}: expected top-level object")
            yield path, 1, value
