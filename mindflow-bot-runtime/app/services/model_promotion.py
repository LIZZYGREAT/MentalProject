"""Single audited entry point for promoting Stage-4 candidates."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
import math
import uuid

from sqlalchemy import select

from app.db import Database
from app.models import (
    DatasetSnapshot,
    DatasetSnapshotItem,
    ModelEvaluationRun,
    ModelPromotionDecision,
)
from app.repositories import LearnedProfileRepository, promotion_parameters_hash
from app.services.model_comparison import MODEL_VARIANT_BY_FAMILY, PROMOTION_GATE_VERSION
from app.services.research_evaluation import EVALUATION_CODE_VERSION, ResearchEvaluationService
from app.services.stage4_candidate_replay import DEPLOYMENT_REFIT_VERSION


class ModelPromotionService:
    CONFIDENCE_VERSION = "stage4-calibration-confidence.v1"

    def __init__(self, database: Database, timezone_name: str):
        self.database = database
        self.timezone_name = timezone_name
        self.learned_profiles = LearnedProfileRepository(database)

    @classmethod
    def _calibration_confidence(
        cls, evidence: dict[str, Any], uncertainty: dict[str, Any]
    ) -> tuple[float, dict[str, Any]]:
        standard_errors: list[float] = []

        def collect(value: Any) -> None:
            if isinstance(value, dict):
                if "std_error" in value:
                    try:
                        number = float(value["std_error"])
                    except (TypeError, ValueError):
                        return
                    if math.isfinite(number) and number >= 0:
                        standard_errors.append(number)
                for child in value.values():
                    collect(child)

        collect(uncertainty)
        sample_component = min(1.0, int(evidence.get("sample_count") or 0) / 42.0)
        day_component = min(1.0, int(evidence.get("day_count") or 0) / 14.0)
        transition_component = min(
            1.0, int(evidence.get("transition_count") or 0) / 12.0
        )
        uncertainty_component = (
            1.0 / (1.0 + sum(standard_errors) / len(standard_errors))
            if standard_errors
            else 0.0
        )
        confidence = min(
            0.95,
            0.35 * sample_component
            + 0.25 * day_component
            + 0.20 * transition_component
            + 0.20 * uncertainty_component,
        )
        definition = {
            "version": cls.CONFIDENCE_VERSION,
            "sample_component": round(sample_component, 6),
            "day_component": round(day_component, 6),
            "transition_component": round(transition_component, 6),
            "uncertainty_component": round(uncertainty_component, 6),
            "maximum": 0.95,
        }
        return round(confidence, 6), definition

    @staticmethod
    def _has_required_parameter_uncertainty(
        uncertainty: dict[str, Any]
    ) -> bool:
        values = [
            (uncertainty.get("S_star_init") or {}).get("std_error"),
            (
                (uncertainty.get("ctssm_params") or {}).get(
                    "workload_stress_gain"
                )
                or {}
            ).get("std_error"),
            (
                (uncertainty.get("ctssm_params") or {}).get(
                    "recovery_stress_gain"
                )
                or {}
            ).get("std_error"),
        ]
        try:
            normalized = [float(value) for value in values]
        except (TypeError, ValueError):
            return False
        return all(math.isfinite(value) and value >= 0 for value in normalized)

    def _validate_manifest(
        self, snapshot: DatasetSnapshot, rows: list[DatasetSnapshotItem]
    ) -> str:
        items = [
            {
                "item_type": row.item_type,
                "source_id": row.source_id,
                "source_version": row.source_version,
                "participant_id": row.participant_id,
                "local_date": row.local_date,
                "source_hash": row.source_hash,
                "metadata": dict(row.metadata_json),
            }
            for row in rows
        ]
        def iso(value: datetime) -> str:
            return (
                value.replace(tzinfo=timezone.utc)
                if value.tzinfo is None
                else value.astimezone(timezone.utc)
            ).isoformat()

        contract = {
            "schema_version": snapshot.schema_version,
            "date_start": snapshot.date_start.isoformat(),
            "date_end": snapshot.date_end.isoformat(),
            "participant_filter": dict(snapshot.participant_filter),
            "observation_cutoff": iso(snapshot.observation_cutoff),
            "calendar_cutoff": iso(snapshot.calendar_cutoff),
        }
        calculated = ResearchEvaluationService._manifest_hash(contract, items)
        manifest = dict(snapshot.manifest_json or {})
        expected = str(manifest.get("manifest_hash") or "")
        if not expected or calculated != expected:
            raise ValueError("dataset snapshot manifest mismatch")
        if manifest.get("schema_version") != snapshot.schema_version:
            raise ValueError("dataset snapshot schema/manifest mismatch")
        expected_counts = {
            "item_count": len(items),
            "observation_count": sum(item["item_type"] == "observation" for item in items),
            "forecast_count": sum(item["item_type"] == "forecast" for item in items),
            "calendar_count": sum(item["item_type"] == "calendar" for item in items),
        }
        if snapshot.schema_version in {
            "mindflow-research-dataset-v3",
            "mindflow-research-dataset-v4",
        }:
            expected_counts["participant_count"] = sum(
                item["item_type"] == "participant" for item in items
            )
        if snapshot.schema_version == "mindflow-research-dataset-v4":
            for item_type in ("psychometric", "daily_review", "slow_state"):
                expected_counts[f"{item_type}_count"] = sum(
                    item["item_type"] == item_type for item in items
                )
        if any(manifest.get(key) != value for key, value in expected_counts.items()):
            raise ValueError("dataset snapshot manifest/items count mismatch")
        return calculated

    def promote_candidate(
        self,
        evaluation_run_id: uuid.UUID,
        *,
        participant_id: uuid.UUID | None = None,
        model_family: str | None = None,
    ) -> dict[str, Any]:
        with self.database.session() as session:
            run = session.get(ModelEvaluationRun, evaluation_run_id)
            if run is None:
                raise ValueError("model evaluation run not found")
            if run.evaluation_mode != "offline_replay" or run.status != "completed":
                raise ValueError("promotion requires a completed offline_replay")
            if run.evaluation_code_version != EVALUATION_CODE_VERSION:
                raise ValueError("unsupported evaluation code version")
            if participant_id is not None and run.participant_id != participant_id:
                raise ValueError(
                    "participant promotion requires participant-specific evaluation run"
                )
            target_participant = participant_id or run.participant_id
            snapshot = session.get(DatasetSnapshot, run.dataset_snapshot_id)
            if snapshot is None:
                raise ValueError("dataset snapshot not found")
            if snapshot.schema_version != "mindflow-research-dataset-v4":
                raise ValueError("Stage-4 promotion requires Dataset Schema v4")
            rows = session.execute(
                select(DatasetSnapshotItem).where(
                    DatasetSnapshotItem.dataset_snapshot_id == snapshot.id
                )
            ).scalars().all()
            manifest_hash = self._validate_manifest(snapshot, rows)
            metrics = dict(run.metrics_json or {})
            run_config = dict(metrics.get("config") or {})
            if run_config.get("manifest_hash") != manifest_hash:
                raise ValueError("evaluation manifest provenance mismatch")
            if run_config.get("initial_state_provenance_complete") is not True:
                raise ValueError(
                    "promotion requires frozen initial-state provenance"
                )
            comparison = dict(metrics.get("comparison") or {})
            promotion = dict(metrics.get("promotion") or {})
            candidates = [
                family
                for family, gate in promotion.items()
                if family in MODEL_VARIANT_BY_FAMILY and bool((gate or {}).get("passed"))
            ]
            family = str(model_family or "").strip()
            if family:
                if family not in candidates:
                    raise ValueError("candidate family did not pass promotion gate")
            else:
                family = min(
                    candidates,
                    key=lambda value: float(
                        (comparison.get(value) or {}).get("mae")
                    ),
                    default="",
                )
            if not family:
                raise ValueError("evaluation run has no passing candidate")
            gate = dict(promotion[family])
            if gate.get("gate_version") != PROMOTION_GATE_VERSION:
                raise ValueError("unsupported promotion gate version")
            if (gate.get("checks") or {}).get(
                "parameter_identifiability"
            ) is not True:
                raise ValueError("candidate did not pass parameter identifiability gate")
            deployment_parameters = dict(
                metrics.get("deployment_parameters") or {}
            )
            deployment_uncertainty = dict(
                metrics.get("deployment_uncertainty") or {}
            )
            deployment_evidence = dict(
                metrics.get("deployment_evidence") or {}
            )
            participant_parameters = (
                dict(deployment_parameters.get(str(target_participant)) or {})
                if target_participant is not None
                else {}
            )
            if target_participant is not None and not participant_parameters:
                raise ValueError("evaluation has no deployment parameters for participant")
            participant_uncertainty = (
                dict(deployment_uncertainty.get(str(target_participant)) or {})
                if target_participant is not None
                else {}
            )
            participant_evidence = (
                dict(deployment_evidence.get(str(target_participant)) or {})
                if target_participant is not None
                else {}
            )
            if target_participant is not None:
                if participant_evidence.get(
                    "deployment_refit_version"
                ) != DEPLOYMENT_REFIT_VERSION:
                    raise ValueError("unsupported deployment refit version")
                if str(participant_evidence.get("dataset_snapshot_id") or "") != str(
                    snapshot.id
                ):
                    raise ValueError("deployment refit snapshot provenance mismatch")
                try:
                    evidence_cutoff = datetime.fromisoformat(
                        str(participant_evidence["knowledge_cutoff"]).replace(
                            "Z", "+00:00"
                        )
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    raise ValueError(
                        "deployment refit knowledge cutoff is missing"
                    ) from exc
                if evidence_cutoff.tzinfo is None:
                    evidence_cutoff = evidence_cutoff.replace(tzinfo=timezone.utc)
                snapshot_cutoff = snapshot.observation_cutoff
                if snapshot_cutoff.tzinfo is None:
                    snapshot_cutoff = snapshot_cutoff.replace(tzinfo=timezone.utc)
                if evidence_cutoff.astimezone(timezone.utc) != snapshot_cutoff.astimezone(
                    timezone.utc
                ):
                    raise ValueError("deployment refit knowledge cutoff mismatch")
            if target_participant is not None and participant_evidence.get(
                "identifiability_status"
            ) not in {"identified", "weak"}:
                raise ValueError(
                    "promotion parameters are not identifiable"
                )
            if target_participant is not None and (
                not self._has_required_parameter_uncertainty(
                    participant_uncertainty
                )
                or not self.learned_profiles._valid_uncertainty(
                    participant_parameters, participant_uncertainty
                )
            ):
                raise ValueError(
                    "promotion requires real candidate parameter uncertainty"
                )
            confidence, confidence_definition = self._calibration_confidence(
                participant_evidence, participant_uncertainty
            )
            now = datetime.now(timezone.utc)
            parameters_hash = promotion_parameters_hash(participant_parameters)
            decision = ModelPromotionDecision(
                model_evaluation_run_id=run.id,
                dataset_snapshot_id=snapshot.id,
                participant_id=target_participant,
                model_family=family,
                promotion_gate_version=PROMOTION_GATE_VERSION,
                evaluation_code_version=EVALUATION_CODE_VERSION,
                parameters_hash=parameters_hash,
                status="retained_from_empirical_evidence",
                passed_at=run.created_at,
                promoted_at=now,
            )
            session.add(decision)
            session.flush()
            decision_id = decision.id
            sample_count = int(participant_evidence.get("sample_count") or 0)
            day_count = int(participant_evidence.get("day_count") or 0)
            refit_start, refit_end = snapshot.date_start, snapshot.date_end
            if target_participant is not None:
                try:
                    refit_start = datetime.fromisoformat(
                        str(participant_evidence["window_start"])
                    ).date()
                    refit_end = datetime.fromisoformat(
                        str(participant_evidence["window_end"])
                    ).date()
                except (KeyError, TypeError, ValueError) as exc:
                    raise ValueError("deployment refit window is missing") from exc
                if (
                    sample_count <= 0
                    or day_count <= 0
                    or refit_start < snapshot.date_start
                    or refit_end > snapshot.date_end
                    or refit_start > refit_end
                ):
                    raise ValueError("deployment refit evidence window is invalid")
            learned_profile = None
            if target_participant is not None:
                variant = MODEL_VARIANT_BY_FAMILY[family]
                promoted_parameters = {
                    **participant_parameters,
                    "model_selection": {
                        "active_variant": variant,
                        "status": "retained_from_empirical_evidence",
                        "promotion_decision_id": str(decision_id),
                        "model_evaluation_run_id": str(evaluation_run_id),
                        "dataset_snapshot_id": str(snapshot.id),
                        "promotion_gate_version": PROMOTION_GATE_VERSION,
                        "evaluation_code_version": EVALUATION_CODE_VERSION,
                        "passed_at": run.created_at.isoformat(),
                        "promoted_at": now.isoformat(),
                        "manifest_hash": manifest_hash,
                        "parameters_hash": parameters_hash,
                        "calibration_confidence": confidence,
                        "calibration_confidence_definition": confidence_definition,
                        "knowledge_cutoff": participant_evidence.get(
                            "knowledge_cutoff"
                        ),
                        "deployment_refit_version": DEPLOYMENT_REFIT_VERSION,
                        "deployment_refit_window": {
                            "start": refit_start.isoformat(),
                            "end": refit_end.isoformat(),
                            "sample_count": sample_count,
                            "day_count": day_count,
                        },
                    },
                }
                learned_profile = self.learned_profiles.save_in_session(
                    session,
                    target_participant,
                    parameters=promoted_parameters,
                    uncertainty=participant_uncertainty,
                    sample_count=sample_count,
                    day_count=day_count,
                    confidence=confidence,
                    window_start=refit_start,
                    window_end=refit_end,
                    source=DEPLOYMENT_REFIT_VERSION,
                    model_version="mindflow-ctssm-runtime-v8",
                    validation_status="validated",
                )
        return {
            "id": str(decision_id),
            "model_evaluation_run_id": str(evaluation_run_id),
            "dataset_snapshot_id": str(snapshot.id),
            "participant_id": str(target_participant) if target_participant else None,
            "model_family": family,
            "status": "retained_from_empirical_evidence",
            "promotion_gate_version": PROMOTION_GATE_VERSION,
            "evaluation_code_version": EVALUATION_CODE_VERSION,
            "passed_at": run.created_at.isoformat(),
            "promoted_at": now.isoformat(),
            "learned_profile": learned_profile,
        }
