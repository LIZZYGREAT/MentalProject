"""Export one provider-neutral prompt packet per Scenario × Module."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from ..loader import load_json, load_jsonl


AI_ANNOTATORS = {"AI-A", "AI-B", "AI-C"}
MODULE_TITLES = {
    "A": ("## 3. Module A", "## 4. Module B"),
    "B": ("## 4. Module B", "## 5. Module C"),
    "C": ("## 5. Module C", "## 6. Critical"),
}


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _manual_excerpt(text: str, module: str) -> str:
    start_marker, end_marker = MODULE_TITLES[module]
    start = text.find(start_marker)
    end = text.find(end_marker, start + len(start_marker))
    if start < 0 or end < 0:
        raise ValueError(f"cannot locate Module {module} section in coding manual")
    return text[start:end].strip()


def _compact_contract(schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": schema["type"],
        "required": schema["required"],
        "properties": schema["properties"],
        "additionalProperties": schema["additionalProperties"],
    }


def export_ai_packets(
    *,
    annotator_id: str,
    annotation_round: str,
    module: str,
    assignment_path: str | Path,
    assignment_manifest_path: str | Path,
    manual_path: str | Path,
    output_schema_path: str | Path,
    output_dir: str | Path,
) -> dict[str, Any]:
    if annotator_id not in AI_ANNOTATORS:
        raise ValueError("prompt packets are only available for AI-A, AI-B, and AI-C")
    if module not in MODULE_TITLES:
        raise ValueError(f"unknown module: {module}")
    manifest = load_json(assignment_manifest_path)
    if manifest.get("annotator_id") != annotator_id:
        raise ValueError("assignment manifest annotator does not match requested annotator")
    if manifest.get("annotation_round") != annotation_round:
        raise ValueError("assignment manifest round does not match requested round")
    scenarios = load_jsonl(assignment_path)
    if any(scenario.get("annotation_modules") != [module] for scenario in scenarios):
        raise ValueError("assignment contains a scenario view for another module")
    manual_bytes = Path(manual_path).read_bytes()
    manual_text = manual_bytes.decode("utf-8")
    output_schema = load_json(output_schema_path)
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    packet_digests: dict[str, str] = {}
    for scenario in scenarios:
        scenario_id = str(scenario["scenario_id"])
        packet = {
            "packet_version": "1.0",
            "packet_id": f"{annotation_round}:{annotator_id}:{module}:{scenario_id}",
            "annotation_round": annotation_round,
            "annotator_id": annotator_id,
            "manual_version": str(manifest["manual_version"]),
            "scenario_version": str(scenario["scenario_version"]),
            "scenario_id": scenario_id,
            "annotation_module": module,
            "manual_excerpt": _manual_excerpt(manual_text, module),
            "scenario_view": scenario,
            "instructions": [
                "Annotate only this scenario and module.",
                "Return exactly one JSON object matching output_contract.",
                "Return no system-owned identifiers, versions, timestamps, or annotator metadata.",
                "Use only evidence visible in scenario_view and respect known_at/send_at boundaries.",
                "Do not infer Gold, latent stress, EMA, forecast, or support effect.",
            ],
            "output_contract": _compact_contract(output_schema),
            "integrity": {
                "manual_sha256": _digest(manual_bytes),
                "scenario_sha256": _digest(
                    json.dumps(scenario, ensure_ascii=False, sort_keys=True).encode("utf-8")
                ),
            },
        }
        encoded = (json.dumps(packet, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        (root / f"{scenario_id}.json").write_bytes(encoded)
        packet_digests[scenario_id] = _digest(encoded)
    export_manifest = {
        "packet_version": "1.0",
        "annotation_round": annotation_round,
        "annotator_id": annotator_id,
        "annotation_module": module,
        "manual_version": manifest["manual_version"],
        "scenario_count": len(scenarios),
        "packet_sha256": packet_digests,
    }
    (root / "manifest.json").write_text(
        json.dumps(export_manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return export_manifest
