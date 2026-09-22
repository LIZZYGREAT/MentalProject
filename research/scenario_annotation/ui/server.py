"""Small loopback-only Starlette application for Stage 1 annotation."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from ..ai_runner.packets import _manual_excerpt
from ..analysis.common import flatten_annotations, load_annotation_documents
from ..drafts import module_targets, save_human_draft
from ..loader import load_json, load_jsonl
from ..validation import Validator
from .fields import module_field_specs


class AnnotationStore:
    """A fixed-path store with no method capable of opening hidden or AI output."""

    def __init__(
        self,
        package_root: str | Path,
        *,
        round_name: str = "calibration",
        annotator_id: str = "Human",
        draft_root: str | Path | None = None,
    ) -> None:
        if round_name != "calibration" or annotator_id != "Human":
            raise ValueError("the current local annotation UI supports Human calibration only")
        self.package_root = Path(package_root).resolve()
        self.assignment_root = (
            self.package_root / "assignments" / "round_calibration" / "human"
        ).resolve()
        self.manual_path = (self.package_root / "manuals" / "coding_manual_v0.1.md").resolve()
        self.schema_root = (self.package_root / "schemas").resolve()
        self.draft_root = Path(
            draft_root
            or self.package_root / "annotations" / "drafts" / "human" / "calibration"
        ).resolve()
        self.allowed_roots = (
            self.assignment_root,
            self.manual_path,
            self.schema_root,
            self.draft_root,
        )
        self.manifest = load_json(self.assignment_root / "manifest.json")
        self.manual_text = self.manual_path.read_text(encoding="utf-8")
        self.scenarios = {
            module: {
                str(row["scenario_id"]): row
                for row in load_jsonl(self.assignment_root / f"module_{module.lower()}.jsonl")
            }
            for module in ("A", "B", "C")
        }

    def draft_path(self, module: str, scenario_id: str) -> Path:
        if module not in self.scenarios or scenario_id not in self.scenarios[module]:
            raise KeyError("scenario/module is not in the Human assignment")
        return self.draft_root / f"module_{module.lower()}" / f"{scenario_id}.json"

    def state(self) -> dict[str, Any]:
        modules: dict[str, Any] = {}
        for module, scenarios in self.scenarios.items():
            rows = []
            for scenario_id in scenarios:
                path = self.draft_path(module, scenario_id)
                draft = load_json(path) if path.exists() else None
                rows.append(
                    {
                        "scenario_id": scenario_id,
                        "status": draft.get("status", "NOT_STARTED") if draft else "NOT_STARTED",
                        "flagged_for_review": bool(draft and draft.get("flagged_for_review")),
                    }
                )
            modules[module] = {
                "items": rows,
                "complete": sum(row["status"] == "COMPLETE" for row in rows),
                "total": len(rows),
            }
        return {"mode": "annotation", "annotator_id": "Human", "round": "calibration", "modules": modules}

    def scenario_payload(self, module: str, scenario_id: str) -> dict[str, Any]:
        scenario = self.scenarios[module][scenario_id]
        path = self.draft_path(module, scenario_id)
        return {
            "scenario": scenario,
            "targets": module_targets(scenario, module),
            "field_specs": module_field_specs(module),
            "manual_excerpt": _manual_excerpt(self.manual_text, module),
            "evidence": _visible_evidence(scenario),
            "draft": load_json(path) if path.exists() else None,
        }

    def save(
        self, module: str, scenario_id: str, value: Mapping[str, Any]
    ) -> dict[str, Any]:
        scenario = self.scenarios[module][scenario_id]
        return save_human_draft(
            payload={
                "scenario_validity": value.get("scenario_validity"),
                "records": value.get("records", []),
            },
            scenario=scenario,
            manifest=self.manifest,
            module=module,
            output_path=self.draft_path(module, scenario_id),
            flagged_for_review=bool(value.get("flagged_for_review")),
            mark_complete=bool(value.get("mark_complete")),
            schema_dir=self.schema_root,
        )


class AdjudicationStore:
    def __init__(self, package_root: str | Path, round_name: str = "calibration") -> None:
        if round_name != "calibration":
            raise ValueError("formal adjudication is unavailable before Gate A")
        self.package_root = Path(package_root).resolve()
        self.analysis_root = self.package_root / "analysis" / "outputs" / "calibration"
        self.annotations_root = self.package_root / "annotations" / "calibration"
        self.output_root = self.package_root / "adjudication" / "drafts" / "calibration"
        required_analysis = {
            "disagreement_queue.jsonl",
            "disagreement_report.md",
            "field_metrics.csv",
            "confusion_matrices.json",
            "critical_violations.jsonl",
            "critical_violation_rates.json",
            "orthogonality.jsonl",
            "scenario_annotation_report.md",
        }
        missing_analysis = sorted(
            name for name in required_analysis if not (self.analysis_root / name).exists()
        )
        if missing_analysis:
            raise RuntimeError("adjudication is unavailable until calibration analysis is complete")
        queue_path = self.analysis_root / "disagreement_queue.jsonl"
        documents = load_annotation_documents(self.annotations_root, validate=False)
        annotators = {str(document["annotator_id"]) for document in documents}
        missing = {"AI-A", "AI-B", "AI-C", "Human"} - annotators
        if missing:
            raise RuntimeError(f"adjudication is unavailable; missing independent annotators {sorted(missing)}")
        submitted = {
            (
                str(document["annotator_id"]),
                str(document["annotation_module"]),
                str(document["scenario_id"]),
            )
            for document in documents
        }
        missing_assignments: list[str] = []
        for annotator_id in ("AI-A", "AI-B", "AI-C", "Human"):
            slug = annotator_id.lower().replace("-", "_")
            assignment_root = self.package_root / "assignments" / "round_calibration" / slug
            for module in ("A", "B", "C"):
                for scenario in load_jsonl(assignment_root / f"module_{module.lower()}.jsonl"):
                    key = (annotator_id, module, str(scenario["scenario_id"]))
                    if key not in submitted:
                        missing_assignments.append(":".join(key))
        if missing_assignments:
            raise RuntimeError(
                "adjudication is unavailable; incomplete independent annotation set "
                f"({len(missing_assignments)} missing)"
            )
        self.queue = load_jsonl(queue_path)
        self.violations = load_jsonl(self.analysis_root / "critical_violations.jsonl")
        self.rows = flatten_annotations(documents)
        self.scenarios = {
            str(row["scenario_id"]): row
            for row in load_jsonl(self.package_root / "scenarios" / "calibration.jsonl")
        }
        self.manual = (self.package_root / "manuals" / "coding_manual_v0.1.md").read_text(encoding="utf-8")

    def state(self) -> dict[str, Any]:
        completed = sum(
            (self.output_root / f"{hashlib.sha256(item['disagreement_id'].encode('utf-8')).hexdigest()[:16]}.json").exists()
            for item in self.queue
        )
        return {
            "mode": "adjudication",
            "round": "calibration",
            "items": self.queue,
            "complete": completed,
            "total": len(self.queue),
        }

    def item(self, index: int) -> dict[str, Any]:
        item = self.queue[index]
        labels = [
            {
                "annotator_id": row.annotator_id,
                "label": row.label,
                "annotation_id": row.annotation_id,
                "evidence_refs": row.record.get("evidence_refs", []),
                "evidence_span": row.record.get("evidence_span"),
            }
            for row in self.rows
            if row.scenario_id == item["scenario_id"]
            and row.target_ref == item["target_ref"]
            and row.variable == item["variable"]
        ]
        label_counts = Counter(str(label["label"]) for label in labels)
        related_violations = [
            violation
            for violation in self.violations
            if violation.get("scenario_id") == item["scenario_id"]
            and item["variable"] in violation.get("affected_fields", [])
        ]
        digest = hashlib.sha256(item["disagreement_id"].encode("utf-8")).hexdigest()[:16]
        saved_path = self.output_root / f"{digest}.json"
        return {
            "queue_item": item,
            "scenario": self.scenarios[item["scenario_id"]],
            "manual": self.manual,
            "independent_labels": labels,
            "agreement": {
                "unanimous": len(label_counts) == 1,
                "label_counts": dict(label_counts),
            },
            "critical_violations": related_violations,
            "saved_decision": load_json(saved_path) if saved_path.exists() else None,
        }

    def save(self, index: int, value: Mapping[str, Any]) -> dict[str, Any]:
        for field in ("final_label", "adjudication_reason", "decision", "adjudicated_by"):
            if value.get(field) in (None, "", []):
                raise ValueError(f"{field} is required")
        if value["decision"] not in {"KEEP", "REVISE", "SIMPLIFY", "DROP"}:
            raise ValueError("invalid decision")
        item = self.queue[index]
        adjudicated_by = value["adjudicated_by"]
        if isinstance(adjudicated_by, str):
            adjudicated_by = [adjudicated_by]
        if not isinstance(adjudicated_by, list) or not all(
            isinstance(person, str) and person.strip() for person in adjudicated_by
        ):
            raise ValueError("adjudicated_by must contain at least one reviewer")
        scenario = self.scenarios[item["scenario_id"]]
        related_rows = [
            row
            for row in self.rows
            if row.scenario_id == item["scenario_id"]
            and row.target_ref == item["target_ref"]
            and row.variable == item["variable"]
        ]
        manual_versions = {str(row.record.get("manual_version")) for row in related_rows}
        if len(manual_versions) != 1:
            raise ValueError("source annotations do not share one manual version")
        decision = {
            "gold_id": f"GOLD:{item['disagreement_id']}",
            "scenario_id": item["scenario_id"],
            "target_ref": item["target_ref"],
            "variable": item["variable"],
            "gold_label": value["final_label"],
            "adjudication_reason": value["adjudication_reason"],
            "decision": value["decision"],
            "manual_version": manual_versions.pop(),
            "scenario_version": scenario["scenario_version"],
            "adjudicated_by": adjudicated_by,
            "adjudicated_at": datetime.now(timezone.utc).isoformat(),
            "source_annotation_ids": item["annotation_ids"],
        }
        digest = hashlib.sha256(item["disagreement_id"].encode("utf-8")).hexdigest()[:16]
        path = self.output_root / f"{digest}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.stem + ".validation.json")
        temporary.write_text(json.dumps(decision, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        try:
            result = Validator().validate_paths([temporary], "adjudication")
            if not result.ok:
                raise ValueError(
                    "adjudication validation failed: "
                    + "; ".join(str(issue) for issue in result.issues)
                )
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        return decision


def _visible_evidence(scenario: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    mappings = (
        ("recent_context", "evidence_ref"),
        ("observed_conversation_evidence", "evidence_ref"),
        ("focal_events", "event_ref"),
        ("current_tasks", "event_ref"),
        ("bot_response_units", "response_unit_ref"),
    )
    seen: set[str] = set()
    for collection, ref_field in mappings:
        for item in scenario.get(collection, []):
            ref = str(item.get(ref_field, ""))
            if not ref or ref in seen:
                continue
            seen.add(ref)
            rows.append(
                {
                    "evidence_ref": ref,
                    "speaker": item.get("speaker", "SYSTEM" if collection == "recent_context" else "OBSERVED"),
                    "known_at": item.get("known_at") or item.get("sent_at"),
                    "text": item.get("text") or item.get("description") or item.get("title", ""),
                }
            )
    return rows


def create_app(
    *,
    package_root: str | Path | None = None,
    mode: str = "annotation",
    round_name: str = "calibration",
    annotator_id: str = "Human",
    draft_root: str | Path | None = None,
) -> Starlette:
    root = Path(package_root or Path(__file__).parents[1]).resolve()
    static_root = Path(__file__).with_name("static")
    if mode == "annotation":
        store: AnnotationStore | AdjudicationStore = AnnotationStore(
            root, round_name=round_name, annotator_id=annotator_id, draft_root=draft_root
        )
    elif mode == "adjudication":
        store = AdjudicationStore(root, round_name=round_name)
    else:
        raise ValueError("mode must be annotation or adjudication")

    async def index(_: Request) -> FileResponse:
        return FileResponse(static_root / "index.html")

    async def state(_: Request) -> JSONResponse:
        return JSONResponse(store.state())

    async def scenario(request: Request) -> JSONResponse:
        if not isinstance(store, AnnotationStore):
            return JSONResponse({"error": "annotation mode required"}, status_code=409)
        try:
            return JSONResponse(store.scenario_payload(request.path_params["module"], request.path_params["scenario_id"]))
        except KeyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)

    async def save_draft(request: Request) -> JSONResponse:
        if not isinstance(store, AnnotationStore):
            return JSONResponse({"error": "annotation mode required"}, status_code=409)
        try:
            saved = store.save(
                request.path_params["module"], request.path_params["scenario_id"], await request.json()
            )
        except (KeyError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)
        return JSONResponse(saved)

    async def adjudication_item(request: Request) -> JSONResponse:
        if not isinstance(store, AdjudicationStore):
            return JSONResponse({"error": "adjudication mode required"}, status_code=409)
        try:
            return JSONResponse(store.item(int(request.path_params["index"])))
        except (IndexError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)

    async def save_adjudication(request: Request) -> JSONResponse:
        if not isinstance(store, AdjudicationStore):
            return JSONResponse({"error": "adjudication mode required"}, status_code=409)
        try:
            return JSONResponse(store.save(int(request.path_params["index"]), await request.json()))
        except (IndexError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)

    app = Starlette(
        routes=[
            Route("/", index),
            Route("/api/state", state),
            Route("/api/scenario/{module:str}/{scenario_id:str}", scenario),
            Route("/api/draft/{module:str}/{scenario_id:str}", save_draft, methods=["POST"]),
            Route("/api/adjudication/item/{index:int}", adjudication_item),
            Route("/api/adjudication/item/{index:int}", save_adjudication, methods=["POST"]),
            Mount("/static", StaticFiles(directory=static_root), name="static"),
        ]
    )
    app.state.annotation_store = store
    return app
