from datetime import date, datetime, timezone
import uuid

import pytest

from app.models import (
    DatasetSnapshot,
    DatasetSnapshotItem,
    LearnedModelProfile,
    ParameterLearningRun,
)
from app.repositories import LearnedProfileRepository
from app.services.hierarchical_personalization import (
    LEARNING_VERSION,
    MODEL_FAMILY,
    RESIDUAL_MAX,
    ParameterLearningService,
    evidence_counts,
    fit_partial_pooling,
    fit_residual_ridge,
    minimum_data_gate,
    predict_residual,
    rolling_personalization_validation,
    runtime_candidate_parameters,
)
from tests.helpers import memory_database, participant


def _samples(*, days=18, per_day=3, participant_id="p", offset=0.0):
    rows = []
    previous = 4.0 + offset
    for day in range(1, days + 1):
        for slot in range(per_day):
            workload = (day + slot) % 5 / 4.0
            recovery = 0.8 if slot == 2 else 0.0
            target = 4.0 + offset + 3.2 * workload - 1.5 * recovery
            stress = 0.55 * previous + 0.45 * target
            rows.append(
                {
                    "participant_id": participant_id,
                    "local_date": date(2030, 1, 1).fromordinal(
                        date(2030, 1, 1).toordinal() + day - 1
                    ).isoformat(),
                    "observed_at": datetime(
                        2030, 1, 1, 8 + slot * 4, tzinfo=timezone.utc
                    ).replace(day=day).isoformat(),
                    "actual_stress": stress,
                    "workload": workload,
                    "recovery": recovery,
                    "continuous_load": workload * 0.5,
                    "observed_vitality": 8.0 - stress * 0.3,
                    "context": {
                        "event_types": ["task"] if workload > 0.5 else [],
                        "courses": ["course-a"] if slot == 1 else [],
                        "semantic_dimensions": {"urgency": workload},
                    },
                }
            )
            previous = stress
    return rows


def test_minimum_gate_and_parameter_identifiability_are_explicit():
    samples = _samples()
    counts = evidence_counts(samples)
    gate = minimum_data_gate(samples)

    assert counts["observed_days"] == 18
    assert counts["matched_ema"] == 54
    assert counts["workload_levels"] >= 3
    assert counts["recovery_episodes"] >= 3
    assert gate["passed"] is True
    assert minimum_data_gate(samples[:20])["passed"] is False


def test_partial_pooling_holds_unidentified_parameters_at_population_prior():
    population = _samples(participant_id="population") + _samples(
        participant_id="population-2", offset=1.0
    )
    sparse = [
        {
            **row,
            "participant_id": "sparse",
            "workload": 0.1,
            "recovery": 0.0,
        }
        for row in _samples(days=2, per_day=2)[:4]
    ]
    fitted = fit_partial_pooling(population, sparse, trait_resilience=0.8)

    assert fitted["method"] == LEARNING_VERSION
    for name in ("workload_sensitivity_i", "stress_reactivity_i", "stress_recovery_i"):
        audit = fitted["uncertainty"][name]
        assert audit["pooling_weight"] == 0.0
        assert audit["evidence_status"] == "population_prior_insufficient_contrast"
        assert "interval_95" in audit
        assert "evidence_strength" in audit
        assert "sample_count" in audit
    assert fitted["parameters"]["stress_recovery_i"] > fitted["population_prior"][
        "stress_recovery_i"
    ]["mean"]


def test_residual_ridge_is_shadow_only_and_strictly_bounded():
    samples = _samples()
    fitted = fit_partial_pooling(samples, samples)
    model = fit_residual_ridge(samples, fitted["parameters"])
    model["intercept"] = 999.0
    correction = predict_residual(samples[-1], model)

    assert model["mode"] == "shadow"
    assert model["residual_max"] == RESIDUAL_MAX
    assert correction == RESIDUAL_MAX
    assert {
        "hour_sin",
        "hour_cos",
        "workload",
        "continuous_load",
        "previous_stress",
        "previous_vitality",
        "recovery_window",
    } <= set(model["feature_names"])


def test_rolling_origin_compares_all_required_models_and_residual_gate():
    samples = _samples(days=19)
    result = rolling_personalization_validation(
        samples,
        samples,
        explicit_parameters={"S_star_init": 70.0},
        current_parameters={"S_star_init": 65.0},
    )

    assert result["rolling_origin"]["minimum_training_days"] == 14
    assert result["rolling_origin"]["split_count"] == 5
    assert {
        "global_model",
        "explicit_profile_model",
        "current_personalized_model",
        "new_candidate_model",
        "candidate_with_residual_shadow",
    } == set(result["comparison"])
    assert {
        "mae",
        "rmse",
        "coverage",
        "peak_timing_error_minutes",
    } <= set(result["comparison"]["new_candidate_model"])
    assert result["residual_gate"]["checks"]["correction_bound"] == 1.0
    assert result["residual_gate"]["checks"]["mode"] == "shadow"


def test_candidate_and_promoted_profiles_are_distinct_and_runtime_audited():
    database = memory_database()
    person = participant(database, "stage5-active")
    snapshot_id = uuid.uuid4()
    run_id = uuid.uuid4()
    fitted = fit_partial_pooling(_samples(), _samples())
    candidate = runtime_candidate_parameters(fitted)
    candidate["residual_model"] = fit_residual_ridge(_samples(), fitted["parameters"])
    uncertainty = {
        "hierarchical_parameters": fitted["uncertainty"],
        "S_star_init": {"std_error": 1.0},
        "ctssm_params": {"std_error": 1.0},
        "hierarchical_population_prior": {"std_error": 1.0},
        "residual_model": {"std_error": 1.0},
    }
    selection = {
        "workflow": "stage5_candidate_active",
        "status": "stage5_candidate",
        "parameter_learning_run_id": str(run_id),
        "dataset_snapshot_id": str(snapshot_id),
    }
    with database.session() as session:
        session.add(
            DatasetSnapshot(
                id=snapshot_id,
                date_start=date(2030, 1, 1),
                date_end=date(2030, 1, 19),
                participant_filter={"participant_codes": ["stage5-active"]},
                observation_cutoff=datetime(2030, 1, 20, tzinfo=timezone.utc),
                calendar_cutoff=datetime(2030, 1, 20, tzinfo=timezone.utc),
                schema_version="test",
                manifest_json={},
            )
        )
        session.add(
            ParameterLearningRun(
                id=run_id,
                participant_id=person.id,
                dataset_snapshot_id=snapshot_id,
                model_family=MODEL_FAMILY,
                parameters_before={},
                parameters_candidate=candidate,
                training_metrics={"minimum_data_gate": {"passed": True, "counts": {"observed_days": 19}}},
                validation_metrics={
                    "promotion_gate": {"passed": True},
                    "comparison": {},
                    "uncertainty": uncertainty,
                },
                sample_count=57,
                status="candidate",
            )
        )
        session.add(
            LearnedModelProfile(
                participant_id=person.id,
                version=1,
                parameters_json={**candidate, "model_selection": selection},
                uncertainty_json=uncertainty,
                source=LEARNING_VERSION,
                model_version="mindflow-ctssm-runtime-v9",
                validation_status="candidate",
                sample_count=57,
                day_count=19,
                confidence=0.9,
                window_start=date(2030, 1, 1),
                window_end=date(2030, 1, 19),
            )
        )

    repository = LearnedProfileRepository(database)
    assert repository.runtime_active(person.id) is None

    result = ParameterLearningService(database, "Asia/Shanghai").promote(run_id)

    assert result["status"] == "promoted"
    active = repository.runtime_active(person.id)
    assert active is not None
    assert active["validation_status"] == "validated"
    assert active["parameters"]["model_selection"]["status"] == "stage5_promoted"
    with database.session() as session:
        profiles = session.execute(
            __import__("sqlalchemy").select(LearnedModelProfile).where(
                LearnedModelProfile.participant_id == person.id
            )
        ).scalars().all()
        assert [row.validation_status for row in profiles] == ["candidate", "validated"]


def test_training_service_persists_run_and_candidate_profile_from_snapshot(monkeypatch):
    database = memory_database()
    person = participant(database, "stage5-training")
    snapshot_id = uuid.uuid4()
    with database.session() as session:
        session.add(
            DatasetSnapshot(
                id=snapshot_id,
                date_start=date(2030, 1, 1),
                date_end=date(2030, 1, 19),
                participant_filter={"participant_codes": ["stage5-training"]},
                observation_cutoff=datetime(2030, 1, 20, tzinfo=timezone.utc),
                calendar_cutoff=datetime(2030, 1, 20, tzinfo=timezone.utc),
                schema_version="test",
                manifest_json={},
            )
        )
        session.add(
            DatasetSnapshotItem(
                dataset_snapshot_id=snapshot_id,
                item_type="participant",
                source_id=str(person.id),
                source_version="participant-membership.v1",
                participant_id=person.id,
                local_date=date(2030, 1, 1),
                source_hash="frozen-participant",
                metadata_json={"participant_id": str(person.id)},
            )
        )
    service = ParameterLearningService(database, "Asia/Shanghai")
    frozen = _samples(days=19, participant_id=str(person.id))
    monkeypatch.setattr(
        service.extractor,
        "_extract",
        lambda items, participant_id: {"samples": frozen},
    )

    result = service.train_snapshot(snapshot_id, person.id)

    assert result["sample_count"] == 57
    assert result["status"] in {"candidate", "rejected"}
    assert result["training_metrics"]["minimum_data_gate"]["passed"] is True
    assert result["validation_metrics"]["rolling_origin"]["split_count"] == 5
    latest = LearnedProfileRepository(database).latest(person.id)
    assert latest["validation_status"] == result["status"]
    assert latest["parameters"]["model_selection"][
        "parameter_learning_run_id"
    ] == result["id"]
