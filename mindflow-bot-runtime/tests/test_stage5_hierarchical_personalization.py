from datetime import date, datetime, timezone
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import (
    DatasetSnapshot,
    DatasetSnapshotItem,
    LearnedModelProfile,
    ParameterLearningRun,
    ParticipantProfile,
)
from app.repositories import (
    LearnedProfileRepository,
    ProfileRepository,
    profile_parameters_hash,
    promotion_parameters_hash,
    stage5_effective_parameters_hash,
)
from app.services.forecast_coordinator import enforce_promoted_model_selection
from app.services.hierarchical_personalization import (
    LEARNING_VERSION,
    MODEL_FAMILY,
    PROMOTION_GATE_VERSION,
    RESIDUAL_MAX,
    ParameterLearningService,
    Stage5PersonalizedReplayService,
    active_learned_profile_as_of,
    _production_current_parameters,
    _causal_previous_features,
    _with_intervention_exclusions,
    estimate_population_prior,
    explicit_profile_as_of,
    evidence_counts,
    fit_partial_pooling,
    fit_residual_ridge,
    minimum_data_gate,
    predict_residual,
    rolling_personalization_validation,
    runtime_candidate_parameters,
    trait_resilience_as_of,
)
from app.services.dataset_snapshot_integrity import DatasetSnapshotIntegrityService
from app.services.research_evaluation import DATASET_SCHEMA_V6, DATASET_SCHEMA_V7
from app.services.research_evaluation import ResearchEvaluationService
from app.services.profile_calibration import layered_profile
from mindflow_core.assessment import AssessmentModel
from tests.helpers import memory_database, participant


def _valid_v7_snapshot_rows(
    snapshot_id, person, *, date_start=date(2030, 1, 1), date_end=date(2030, 1, 19)
):
    cutoff = datetime(2030, 1, 20, tzinfo=timezone.utc)
    participant_filter = {"participant_codes": [person.participant_code]}
    metadata = {
        "participant_id": str(person.id),
        "participant_code": person.participant_code,
        "joined_at": datetime(2029, 1, 1, tzinfo=timezone.utc).isoformat(),
        "status_at_snapshot": person.status,
    }
    item_view = {
        "item_type": "participant",
        "source_id": str(person.id),
        "source_version": "participant-membership.v1",
        "participant_id": person.id,
        "local_date": date_start,
        "source_hash": "frozen-participant-source-hash",
        "metadata": metadata,
    }
    contract = {
        "schema_version": DATASET_SCHEMA_V7,
        "date_start": date_start.isoformat(),
        "date_end": date_end.isoformat(),
        "purpose": "manual_research",
        "schedule_key": None,
        "participant_filter": participant_filter,
        "observation_cutoff": cutoff.isoformat(),
        "calendar_cutoff": cutoff.isoformat(),
    }
    manifest = {
        "schema_version": DATASET_SCHEMA_V7,
        "purpose": "manual_research",
        "schedule_key": None,
        "participant_count": 1,
        "observation_count": 0,
        "forecast_count": 0,
        "calendar_count": 0,
        "psychometric_count": 0,
        "daily_review_count": 0,
        "slow_state_count": 0,
        "care_intervention_exposure_count": 0,
        "warning_delivery_count": 0,
        "participant_profile_count": 0,
        "learned_model_profile_count": 0,
        "item_count": 1,
        "manifest_hash": DatasetSnapshotIntegrityService.manifest_hash(
            contract, [item_view]
        ),
    }
    return (
        DatasetSnapshot(
            id=snapshot_id,
            date_start=date_start,
            date_end=date_end,
            purpose="manual_research",
            schedule_key=None,
            participant_filter=participant_filter,
            observation_cutoff=cutoff,
            calendar_cutoff=cutoff,
            schema_version=DATASET_SCHEMA_V7,
            manifest_json=manifest,
        ),
        DatasetSnapshotItem(
            dataset_snapshot_id=snapshot_id,
            item_type=item_view["item_type"],
            source_id=item_view["source_id"],
            source_version=item_view["source_version"],
            participant_id=item_view["participant_id"],
            local_date=item_view["local_date"],
            source_hash=item_view["source_hash"],
            metadata_json=metadata,
        ),
    )


def _validated_effective_profile(candidate, explicit=None, *, variant="m0"):
    explicit = dict(explicit or {})
    identity = {
        "profile_id": None,
        "version": None,
        "created_at": None,
        "parameters_hash": profile_parameters_hash(explicit),
        "source": None,
    }
    return {
        "version": "stage5-effective-profile-provenance.v1",
        "passed": True,
        "snapshot_cutoff": datetime(
            2030, 1, 20, tzinfo=timezone.utc
        ).isoformat(),
        "explicit_profile_identity": identity,
        "explicit_profile_provenance": {
            **identity,
            "usage": "deployment_explicit_profile",
        },
        "validated_effective_parameters_hash": stage5_effective_parameters_hash(
            _production_current_parameters(candidate, explicit), variant
        ),
    }


def _seed_promotable_m0_candidate(database, person):
    service = ParameterLearningService(database, "Asia/Shanghai")
    snapshot_id = uuid.uuid4()
    run_id = uuid.uuid4()
    fitted = fit_partial_pooling(_samples(), _samples())
    candidate = runtime_candidate_parameters(fitted)
    candidate["residual_model"] = {
        "mode": "shadow",
        "residual_sd": 0.4,
    }
    uncertainty = {
        "hierarchical_parameters": fitted["uncertainty"],
        "S_star_init": {"std_error": 1.0},
        "ctssm_params": {"std_error": 1.0},
        "hierarchical_population_prior": {"std_error": 1.0},
        "residual_model": {"std_error": 0.4},
    }
    deployment_identity = service._active_identity(None)
    family_evidence = {
        "passed": True,
        "reason": "snapshot_cutoff_active_matches_train_time_live",
        "snapshot_cutoff": datetime(
            2030, 1, 20, tzinfo=timezone.utc
        ).isoformat(),
        "snapshot_cutoff_active_identity": deployment_identity,
        "train_time_live_active_identity": deployment_identity,
    }
    effective_evidence = _validated_effective_profile(candidate)
    selection = {
        "workflow": "stage5_candidate_active",
        "status": "stage5_candidate",
        "parameter_learning_run_id": str(run_id),
        "dataset_snapshot_id": str(snapshot_id),
        "promotion_gate_version": PROMOTION_GATE_VERSION,
        "active_variant": "m0",
        "model_spec_version": deployment_identity["model_spec_version"],
        "validated_effective_parameters_hash": effective_evidence[
            "validated_effective_parameters_hash"
        ],
        "parameters_hash": effective_evidence[
            "validated_effective_parameters_hash"
        ],
        "explicit_profile_identity": effective_evidence[
            "explicit_profile_identity"
        ],
    }
    with database.session() as session:
        session.add_all(_valid_v7_snapshot_rows(snapshot_id, person))
        session.add(
            ParameterLearningRun(
                id=run_id,
                participant_id=person.id,
                dataset_snapshot_id=snapshot_id,
                model_family=MODEL_FAMILY,
                parameters_before={},
                parameters_candidate=candidate,
                training_metrics={
                    "minimum_data_gate": {
                        "passed": True,
                        "counts": {"observed_days": 19},
                    },
                    "base_active_identity": service._active_identity(None),
                    "deployment_family_evidence": family_evidence,
                    "validated_effective_profile": effective_evidence,
                },
                validation_metrics={
                    "promotion_gate": {
                        "passed": True,
                        "formal_promotion_eligible": True,
                        "version": PROMOTION_GATE_VERSION,
                    },
                    "formal_replay_audit": {
                        "engine": service.formal_replay.FORMAL_ENGINE_VERSION
                    },
                    "uncertainty": uncertainty,
                    "deployment_family_gate": family_evidence,
                    "validated_effective_profile": effective_evidence,
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
                model_version="mindflow-ctssm-runtime-v11",
                validation_status="candidate",
                sample_count=57,
                day_count=19,
                confidence=0.9,
                window_start=date(2030, 1, 1),
                window_end=date(2030, 1, 19),
            )
        )
    return service, run_id, snapshot_id


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
    for name in (
        "workload_sensitivity_i",
        "recovery_sensitivity_i",
        "stress_reactivity_i",
        "stress_recovery_rate_i",
    ):
        audit = fitted["uncertainty"][name]
        assert audit["pooling_weight"] == 0.0
        assert audit["evidence_status"] == "population_prior_insufficient_contrast"
        assert "interval_95" in audit
        assert "evidence_strength" in audit
        assert "sample_count" in audit
    assert fitted["parameters"]["stress_recovery_rate_i"] > fitted["population_prior"][
        "stress_recovery_rate_i"
    ]["mean"]
    assert fitted["parameters"]["recovery_sensitivity_i"] > fitted["population_prior"][
        "recovery_sensitivity_i"
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


def test_recovery_equilibrium_coefficient_and_recovery_rate_map_independently():
    runtime = runtime_candidate_parameters(
        {
            "parameters": {
                "S_star_i": 4.5,
                "workload_sensitivity_i": 2.3,
                "recovery_sensitivity_i": 1.7,
                "stress_reactivity_i": 0.8,
                "stress_recovery_rate_i": 0.3,
            },
            "population_prior": {},
            "trait_resilience": None,
        }
    )

    assert runtime["ctssm_params"]["recovery_stress_gain"] == 17.0
    assert runtime["ctssm_params"]["stress_recovery_per_hour"] == 0.3
    assert runtime["hierarchical_parameters"]["recovery_sensitivity_i"] == 1.7
    assert runtime["hierarchical_parameters"]["stress_recovery_rate_i"] == 0.3


def test_population_prior_is_lopo_and_fails_back_when_peers_are_insufficient():
    target = _samples(participant_id="target", offset=4.0)
    peer = _samples(participant_id="peer", offset=-1.0)
    cutoff = datetime(2030, 2, 1, tzinfo=timezone.utc)

    prior = estimate_population_prior(
        target + peer,
        target_participant_id="target",
        knowledge_cutoff=cutoff,
    )

    assert prior["_metadata"]["target_excluded"] is True
    assert prior["_metadata"]["peer_participant_count"] == 1
    assert prior["_metadata"]["source"] == (
        "versioned_global_default_insufficient_peers"
    )
    assert all(prior[name]["participant_count"] == 0 for name in (
        "S_star_i",
        "workload_sensitivity_i",
        "recovery_sensitivity_i",
        "stress_reactivity_i",
        "stress_recovery_rate_i",
    ))


def test_population_prior_excludes_late_backfill_at_split_cutoff():
    target = _samples(participant_id="target")
    peer_a = _samples(participant_id="peer-a")
    peer_b = _samples(participant_id="peer-b")
    peer_b[-1] = {
        **peer_b[-1],
        "observation_created_at": "2030-02-02T00:00:00+00:00",
    }

    prior = estimate_population_prior(
        target + peer_a + peer_b,
        target_participant_id="target",
        knowledge_cutoff=datetime(2030, 2, 1, tzinfo=timezone.utc),
    )

    assert prior["_metadata"]["peer_participant_count"] == 2
    assert prior["_metadata"]["peer_sample_count"] == len(peer_a) + len(peer_b) - 1
    assert prior["_metadata"]["source"] == "leave_one_participant_out_peers"


def test_future_created_observation_cannot_enter_previous_outcome_features():
    sample = {
        "observed_at": "2030-01-10T10:00:00+00:00",
        "initial_state": {"stress_0_10": 2.0, "vitality_0_10": 8.0},
        "initial_state_revision": "frozen-r1",
    }
    history = [
        {
            "observation_id": "eligible",
            "observed_at": "2030-01-10T08:00:00+00:00",
            "created_at": "2030-01-10T08:05:00+00:00",
            "payload": {"stress_0_10": 4.0, "energy_0_10": 6.0},
        },
        {
            "observation_id": "future-created",
            "observed_at": "2030-01-10T09:00:00+00:00",
            "created_at": "2030-01-10T11:00:00+00:00",
            "payload": {"stress_0_10": 10.0, "energy_0_10": 1.0},
        },
    ]

    features = _causal_previous_features(sample, history)

    assert features["previous_stress"] == 4.0
    assert features["previous_vitality"] == 6.0
    assert features["previous_feature_provenance"]["stress"][
        "observation_id"
    ] == "eligible"


def test_post_intervention_rows_are_versioned_and_excluded_from_core_fit():
    participant_id = uuid.uuid4()
    samples = [
        {
            **_samples(days=1, per_day=2, participant_id=str(participant_id))[0],
            "observed_at": "2030-01-01T10:30:00+00:00",
        },
        {
            **_samples(days=1, per_day=2, participant_id=str(participant_id))[1],
            "observed_at": "2030-01-01T13:30:00+00:00",
        },
    ]
    items = [
        {
            "item_type": "care_intervention_exposure",
            "source_id": "care-1",
            "participant_id": participant_id,
            "metadata": {
                "sent_at": "2030-01-01T10:00:00+00:00",
                "intervention_type": "micro_break",
            },
        }
    ]

    marked, audit = _with_intervention_exclusions(samples, items)

    assert marked[0]["exclude_from_natural_dynamics_fit"] is True
    assert marked[1]["exclude_from_natural_dynamics_fit"] is False
    assert audit["window_minutes"] == 120
    assert audit["policy_version"].endswith(".v1")


def test_brs_and_explicit_profile_resolve_at_strict_knowledge_cutoff():
    participant_id = uuid.uuid4()
    items = [
        {
            "item_type": "psychometric",
            "source_id": "brs-day-10",
            "participant_id": participant_id,
            "metadata": {
                "assessment_id": "brs-day-10",
                "instrument_name": "BRS",
                "scores": {"mean": 4.0},
                "administered_at": "2030-01-10T08:00:00+00:00",
                "created_at": "2030-01-10T09:00:00+00:00",
            },
        },
        {
            "item_type": "psychometric",
            "source_id": "brs-day-20",
            "participant_id": participant_id,
            "metadata": {
                "assessment_id": "brs-day-20",
                "instrument_name": "BRS",
                "scores": {"mean": 1.0},
                "administered_at": "2030-01-20T08:00:00+00:00",
                "created_at": "2030-01-20T09:00:00+00:00",
            },
        },
        {
            "item_type": "participant_profile",
            "source_id": "profile-v1",
            "participant_id": participant_id,
            "metadata": {
                "profile_id": "profile-v1",
                "version": 1,
                "model_params": {"S_star_init": 45.0},
                "parameters_hash": "hash-v1",
                "created_at": "2030-01-05T08:00:00+00:00",
            },
        },
        {
            "item_type": "participant_profile",
            "source_id": "profile-v2",
            "participant_id": participant_id,
            "metadata": {
                "profile_id": "profile-v2",
                "version": 2,
                "model_params": {"S_star_init": 90.0},
                "parameters_hash": "hash-v2",
                "created_at": "2030-01-20T08:00:00+00:00",
            },
        },
    ]

    resilience, brs_audit = trait_resilience_as_of(
        items, participant_id, datetime(2030, 1, 15, tzinfo=timezone.utc)
    )
    explicit, profile_audit = explicit_profile_as_of(
        items, participant_id, datetime(2030, 1, 15, tzinfo=timezone.utc)
    )
    explicit_day_21, profile_day_21 = explicit_profile_as_of(
        items, participant_id, datetime(2030, 1, 21, tzinfo=timezone.utc)
    )

    assert resilience == 0.75
    assert brs_audit["assessment_id"] == "brs-day-10"
    assert brs_audit["available_at"] == "2030-01-10T09:00:00+00:00"
    assert explicit == {"S_star_init": 45.0}
    assert profile_audit["profile_id"] == "profile-v1"
    assert explicit_day_21 == {"S_star_init": 90.0}
    assert profile_day_21["profile_id"] == "profile-v2"


def test_active_learned_profile_resolves_at_split_origin_and_falls_back_m0():
    participant_id = uuid.uuid4()

    def item(profile_id, version, created_at, baseline, variant):
        parameters = {
            "S_star_init": baseline,
            "model_selection": {"active_variant": variant},
        }
        return {
            "item_type": "learned_model_profile",
            "source_id": profile_id,
            "participant_id": participant_id,
            "metadata": {
                "profile_id": profile_id,
                "version": version,
                "parameters": parameters,
                "model_selection": parameters["model_selection"],
                "parameters_hash": f"hash-{profile_id}",
                "model_version": "mindflow-ctssm-runtime-v11",
                "validation_status": "validated",
                "source": "stage5-promoted",
                "created_at": created_at,
                "active_variant": variant,
                "runtime_valid": True,
                "runtime_validation": {"runtime_valid": True},
            },
        }

    items = [
        item("active-v1", 1, "2030-01-10T08:00:00+00:00", 45.0, "m1"),
        item("active-v2", 2, "2030-01-20T08:00:00+00:00", 75.0, "m3"),
    ]
    fallback, fallback_audit = active_learned_profile_as_of(
        items, participant_id, datetime(2030, 1, 10, 8, tzinfo=timezone.utc)
    )
    day_15, day_15_audit = active_learned_profile_as_of(
        items, participant_id, datetime(2030, 1, 15, tzinfo=timezone.utc)
    )
    day_21, day_21_audit = active_learned_profile_as_of(
        items, participant_id, datetime(2030, 1, 21, tzinfo=timezone.utc)
    )

    assert fallback == {}
    assert fallback_audit["active_variant"] == "m0"
    assert fallback_audit["fallback"] is True
    assert day_15["S_star_init"] == 45.0
    assert day_15_audit["profile_id"] == "active-v1"
    assert day_15_audit["active_variant"] == "m1"
    assert day_21["S_star_init"] == 75.0
    assert day_21_audit["profile_id"] == "active-v2"
    assert day_21_audit["active_variant"] == "m3"


def test_current_comparator_uses_exact_production_layering_without_population():
    sparse_learned = {
        "ctssm_params": {"stress_reactivity_per_hour": 0.8},
        "model_selection": {"active_variant": "m1"},
    }
    explicit = {
        "S_star_init": 61.0,
        "ctssm_params": {"vitality_baseline": 68.0},
    }
    expected, _layers = layered_profile(
        {"version": 2, "profile": {"model_params": explicit}},
        {"version": 3, "parameters": sparse_learned},
    )

    current = _production_current_parameters(sparse_learned, explicit)
    fallback = _production_current_parameters({}, {})

    assert current == expected["model_params"]
    assert "workload_stress_gain" not in current.get("ctssm_params", {})
    assert fallback == {}


def test_dataset_v7_freezes_explicit_and_learned_profile_history():
    database = memory_database()
    person = participant(database, "stage5-explicit-history")
    with database.session() as session:
        session.add_all(
            [
                ParticipantProfile(
                    participant_id=person.id,
                    version=1,
                    profile_json={"model_params": {"S_star_init": 45.0}},
                    created_at=datetime(2030, 1, 5, tzinfo=timezone.utc),
                ),
                ParticipantProfile(
                    participant_id=person.id,
                    version=2,
                    profile_json={"model_params": {"S_star_init": 65.0}},
                    created_at=datetime(2030, 1, 20, tzinfo=timezone.utc),
                ),
                LearnedModelProfile(
                    participant_id=person.id,
                    version=1,
                    parameters_json={
                        "S_star_init": 42.0,
                        "model_selection": {"active_variant": "m0"},
                    },
                    uncertainty_json={},
                    source="stage4-calibration",
                    model_version="mindflow-ctssm-runtime-v8",
                    validation_status="validated",
                    sample_count=30,
                    day_count=14,
                    confidence=0.8,
                    window_start=date(2029, 12, 1),
                    window_end=date(2029, 12, 31),
                    created_at=datetime(2030, 1, 10, tzinfo=timezone.utc),
                ),
            ]
        )

    service = ResearchEvaluationService(database, "Asia/Shanghai")
    snapshot = service.create_dataset_snapshot(
        date_start=date(2030, 1, 1),
        date_end=date(2030, 1, 25),
        participant_filter={"participant_codes": [person.participant_code]},
        observation_cutoff=datetime(2030, 1, 26, tzinfo=timezone.utc),
        calendar_cutoff=datetime(2030, 1, 26, tzinfo=timezone.utc),
    )
    profile_items = [
        item
        for item in service.snapshot_items(uuid.UUID(snapshot["id"]))
        if item["item_type"] == "participant_profile"
    ]
    learned_items = [
        item
        for item in service.snapshot_items(uuid.UUID(snapshot["id"]))
        if item["item_type"] == "learned_model_profile"
    ]

    assert snapshot["schema_version"] == DATASET_SCHEMA_V7
    assert snapshot["manifest"]["participant_profile_count"] == 2
    assert snapshot["manifest"]["learned_model_profile_count"] == 1
    assert sorted(item["metadata"]["version"] for item in profile_items) == [1, 2]
    assert profile_items[0]["metadata"]["parameters_hash"]
    assert learned_items[0]["metadata"]["runtime_valid"] is True
    assert learned_items[0]["metadata"]["active_variant"] == "m0"
    assert learned_items[0]["metadata"]["parameters_hash"]


@pytest.mark.parametrize("variant", ["m0", "m1", "m2", "m3"])
def test_formal_validator_calls_production_replay_and_preserves_family(
    variant, monkeypatch
):
    import app.services.hierarchical_personalization as stage5

    def forbidden_surrogate(*args, **kwargs):
        raise AssertionError("formal validation must not call _predict_base")

    monkeypatch.setattr(stage5, "_predict_base", forbidden_surrogate)
    target_id = uuid.uuid4()
    target = []
    calendars = {}
    for index, row in enumerate(_samples(days=15, participant_id=str(target_id))):
        forecast_id = f"forecast-{index}"
        target.append(
            {
                **row,
                "forecast_id": forecast_id,
                "observation_created_at": row["observed_at"],
                "initial_state": {"stress_0_10": 4.0, "vitality_0_10": 7.0},
                "initial_state_revision": "initial-r1",
                "context": {**row["context"], "forecast_point_time": "08:00"},
            }
        )
        calendars[forecast_id] = {"calendar_representation": []}
    population = target + _samples(participant_id="peer-a") + _samples(
        participant_id="peer-b", offset=0.5
    )

    class FakeModel:
        MODEL_SPEC_VERSION = "fake-production-spec"

        def __init__(self):
            self.calls = 0

        def _result(self, baseline, model_variant):
            self.calls += 1
            return SimpleNamespace(
                model_variant=model_variant,
                trajectory=[
                    {
                        "time": "08:00",
                        "stress_0_10": baseline,
                        "stress_interval_90_0_10": {
                            "lower": max(0.0, baseline - 1.0),
                            "upper": min(10.0, baseline + 1.0),
                        },
                    },
                    {"time": "12:00", "stress_0_10": baseline + 0.2},
                ],
            )

        def predict_baseline_m0(self, **kwargs):
            baseline = float(
                kwargs["baseline_params"].get("S_star_init", 50.0)
            ) / 10.0
            return self._result(baseline, "m0")

        def predict_candidate(self, **kwargs):
            baseline = float(
                kwargs["candidate_params"].get("S_star_init", 50.0)
            ) / 10.0
            return self._result(baseline, kwargs["model_variant"])

    extractor = SimpleNamespace(
        model=FakeModel(),
        timezone=timezone.utc,
    )
    # Bind the exact Stage-4 frozen-input helpers used in production.
    from app.services.stage4_candidate_replay import Stage4CandidateReplayService

    extractor._known_observations = Stage4CandidateReplayService._known_observations
    extractor._calendar_events = Stage4CandidateReplayService._calendar_events
    extractor._frozen_initial_state = Stage4CandidateReplayService._frozen_initial_state
    service = Stage5PersonalizedReplayService(extractor)
    historical_active = {
        "item_type": "learned_model_profile",
        "source_id": f"historical-active-{variant}",
        "participant_id": target_id,
        "metadata": {
            "profile_id": f"historical-active-{variant}",
            "version": 1,
            "parameters": {
                "model_selection": {"active_variant": variant}
            },
            "model_selection": {"active_variant": variant},
            "parameters_hash": f"historical-active-{variant}-hash",
            "model_version": "mindflow-ctssm-runtime-v11",
            "validation_status": "validated",
            "source": "test-stage5-active-history",
            "created_at": "2029-12-31T08:00:00+00:00",
            "active_variant": variant,
            "runtime_valid": True,
            "runtime_validation": {
                "runtime_valid": True,
                "reason": "frozen_test_provenance",
            },
        },
    }
    result = service.validate(
        frozen={
            "samples": population,
            "calendars": calendars,
            "observation_history": {str(target_id): []},
        },
        items=[historical_active],
        participant_id=target_id,
    )

    assert extractor.model.calls > 0
    assert result["formal_replay_audit"]["surrogate_predict_base_used"] is False
    assert set(result["formal_replay_audit"]["comparator_variants"].values()) == {
        variant
    }
    assert result["formal_replay_audit"]["population_prior_splits"][0][
        "target_excluded"
    ] is True
    assert result["residual_gate"]["passed"] is False
    assert result["residual_gate"]["formal_residual_promotion_eligible"] is False
    assert result["residual_gate"]["reason"] == (
        "trajectory_and_interval_not_recomputed"
    )
    assert result["residual_gate"]["checks"]["coverage_not_decreased"] is None
    assert result["residual_gate"]["checks"]["peak_error_not_worse"] is None
    assert result["residual_gate"]["checks"][
        "formal_residual_promotion_eligible"
    ] is False

    if variant == "m1":
        extreme_population = target + _samples(
            participant_id="peer-a", offset=3.5
        ) + _samples(participant_id="peer-b", offset=4.0)
        extreme_prior = service.validate(
            frozen={
                "samples": extreme_population,
                "calendars": calendars,
                "observation_history": {str(target_id): []},
            },
            items=[historical_active],
            participant_id=target_id,
        )
        assert result["comparison"]["current_personalized_model"] == (
            extreme_prior["comparison"]["current_personalized_model"]
        )
        assert result["comparison"]["global_model"] != extreme_prior[
            "comparison"
        ]["global_model"]

        def future_items(brs_score, explicit_baseline):
            return [
                {
                    "item_type": "psychometric",
                    "source_id": "future-brs",
                    "participant_id": target_id,
                    "metadata": {
                        "assessment_id": "future-brs",
                        "instrument_name": "BRS",
                        "scores": {"mean": brs_score},
                        "administered_at": "2030-01-20T08:00:00+00:00",
                        "created_at": "2030-01-20T09:00:00+00:00",
                    },
                },
                {
                    "item_type": "participant_profile",
                    "source_id": "future-profile",
                    "participant_id": target_id,
                    "metadata": {
                        "profile_id": "future-profile",
                        "version": 2,
                        "model_params": {"S_star_init": explicit_baseline},
                        "parameters_hash": f"future-{explicit_baseline}",
                        "created_at": "2030-01-20T08:00:00+00:00",
                    },
                },
                {
                    "item_type": "learned_model_profile",
                    "source_id": f"future-active-{explicit_baseline}",
                    "participant_id": target_id,
                    "metadata": {
                        "profile_id": f"future-active-{explicit_baseline}",
                        "version": 2,
                        "parameters": {
                            "S_star_init": explicit_baseline,
                            "model_selection": {
                                "active_variant": (
                                    "m2" if explicit_baseline < 50 else "m3"
                                )
                            },
                        },
                        "model_selection": {
                            "active_variant": (
                                "m2" if explicit_baseline < 50 else "m3"
                            )
                        },
                        "parameters_hash": f"future-active-{explicit_baseline}",
                        "model_version": "mindflow-ctssm-runtime-v11",
                        "validation_status": "validated",
                        "source": "future-stage5-active",
                        "created_at": "2030-01-20T08:00:00+00:00",
                        "active_variant": (
                            "m2" if explicit_baseline < 50 else "m3"
                        ),
                        "runtime_valid": True,
                        "runtime_validation": {"runtime_valid": True},
                    },
                },
            ]

        common = {
            "frozen": {
                "samples": population,
                "calendars": calendars,
                "observation_history": {str(target_id): []},
            },
            "participant_id": target_id,
        }
        future_low = service.validate(
            **common, items=[historical_active, *future_items(1.0, 20.0)]
        )
        future_high = service.validate(
            **common, items=[historical_active, *future_items(5.0, 90.0)]
        )

        assert future_low["rolling_origin"]["splits"][0] == future_high[
            "rolling_origin"
        ]["splits"][0]
        assert future_low["comparison"] == future_high["comparison"]
        assert future_low["promotion_gate"] == future_high["promotion_gate"]
        assert future_low["formal_replay_audit"][
            "active_variant_by_split"
        ] == future_high["formal_replay_audit"]["active_variant_by_split"]
        assert future_low["latest_fit"]["parameters"] == future_high[
            "latest_fit"
        ]["parameters"]
        information_set = future_low["formal_replay_audit"][
            "split_information_sets"
        ][0]
        assert information_set["trait_resilience"]["assessment_id"] is None
        assert information_set["explicit_profile"]["profile_id"] is None
        assert information_set["current_learned_profile"]["profile_id"] == (
            historical_active["metadata"]["profile_id"]
        )
        assert information_set["current_learned_profile"][
            "active_variant"
        ] == "m1"

        causal_items = [historical_active, *future_items(1.0, 90.0),
            {
                "item_type": "psychometric",
                "source_id": "past-brs",
                "participant_id": target_id,
                "metadata": {
                    "assessment_id": "past-brs",
                    "instrument_name": "BRS",
                    "scores": {"mean": 4.0},
                    "administered_at": "2030-01-10T08:00:00+00:00",
                    "created_at": "2030-01-10T09:00:00+00:00",
                },
            },
            {
                "item_type": "participant_profile",
                "source_id": "past-profile",
                "participant_id": target_id,
                "metadata": {
                    "profile_id": "past-profile",
                    "version": 1,
                    "model_params": {"S_star_init": 45.0},
                    "parameters_hash": "past-profile-hash",
                    "created_at": "2030-01-05T08:00:00+00:00",
                },
            },
        ]
        causal = service.validate(**common, items=causal_items)
        causal_information = causal["formal_replay_audit"][
            "split_information_sets"
        ][0]
        assert causal_information["trait_resilience"]["assessment_id"] == "past-brs"
        assert causal_information["trait_resilience"]["prior_version"].endswith(
            ".v1"
        )
        assert causal_information["explicit_profile"]["profile_id"] == (
            "past-profile"
        )


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
    assert result["residual_gate"]["checks"]["coverage_not_decreased"] is None
    assert result["residual_gate"]["checks"]["peak_error_not_worse"] is None
    assert result["residual_gate"]["passed"] is False
    assert result["residual_gate"]["formal_residual_promotion_eligible"] is False
    assert result["promotion_gate"]["passed"] is False
    assert result["promotion_gate"]["formal_promotion_eligible"] is False


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
    service = ParameterLearningService(database, "Asia/Shanghai")
    deployment_identity = service._active_identity(None)
    deployment_family_evidence = {
        "passed": True,
        "reason": "snapshot_cutoff_active_matches_train_time_live",
        "snapshot_cutoff": datetime(
            2030, 1, 20, tzinfo=timezone.utc
        ).isoformat(),
        "snapshot_cutoff_active_identity": deployment_identity,
        "train_time_live_active_identity": deployment_identity,
    }
    effective_profile_evidence = _validated_effective_profile(candidate)
    selection = {
        "workflow": "stage5_candidate_active",
        "status": "stage5_candidate",
        "parameter_learning_run_id": str(run_id),
        "dataset_snapshot_id": str(snapshot_id),
        "promotion_gate_version": PROMOTION_GATE_VERSION,
        "active_variant": deployment_identity["active_variant"],
        "model_spec_version": deployment_identity["model_spec_version"],
        "validated_effective_parameters_hash": effective_profile_evidence[
            "validated_effective_parameters_hash"
        ],
        "parameters_hash": effective_profile_evidence[
            "validated_effective_parameters_hash"
        ],
        "explicit_profile_identity": effective_profile_evidence[
            "explicit_profile_identity"
        ],
    }
    with database.session() as session:
        session.add_all(_valid_v7_snapshot_rows(snapshot_id, person))
        session.add(
            ParameterLearningRun(
                id=run_id,
                participant_id=person.id,
                dataset_snapshot_id=snapshot_id,
                model_family=MODEL_FAMILY,
                parameters_before={},
                parameters_candidate=candidate,
                    training_metrics={
                        "minimum_data_gate": {"passed": True, "counts": {"observed_days": 19}},
                        "base_active_identity": service._active_identity(None),
                        "deployment_family_evidence": deployment_family_evidence,
                        "validated_effective_profile": effective_profile_evidence,
                    },
                validation_metrics={
                    "promotion_gate": {
                        "passed": True,
                        "formal_promotion_eligible": True,
                        "version": PROMOTION_GATE_VERSION,
                    },
                    "formal_replay_audit": {
                        "engine": service.formal_replay.FORMAL_ENGINE_VERSION
                    },
                    "comparison": {},
                    "uncertainty": uncertainty,
                    "deployment_family_gate": deployment_family_evidence,
                    "validated_effective_profile": effective_profile_evidence,
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

    result = service.promote(run_id)

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
    with pytest.raises(ValueError, match="only a candidate"):
        service.promote(run_id)
    with database.session() as session:
        active_profiles = session.execute(
            __import__("sqlalchemy").select(LearnedModelProfile).where(
                LearnedModelProfile.participant_id == person.id,
                LearnedModelProfile.validation_status == "validated",
            )
        ).scalars().all()
        assert len(active_profiles) == 1


def test_runtime_active_requires_v7_and_falls_back_from_v10_or_v11_v6():
    database = memory_database()
    person = participant(database, "stage5-runtime-provenance")
    old_snapshot_id = uuid.uuid4()
    old_run_id = uuid.uuid4()
    old_candidate = {"S_star_init": 60.0}
    with database.session() as session:
        session.add_all(
            [
                DatasetSnapshot(
                    id=old_snapshot_id,
                    date_start=date(2030, 1, 1),
                    date_end=date(2030, 1, 19),
                    participant_filter={},
                    observation_cutoff=datetime(
                        2030, 1, 20, tzinfo=timezone.utc
                    ),
                    calendar_cutoff=datetime(
                        2030, 1, 20, tzinfo=timezone.utc
                    ),
                    schema_version="mindflow-research-dataset-v5",
                    manifest_json={},
                ),
                ParameterLearningRun(
                    id=old_run_id,
                    participant_id=person.id,
                    dataset_snapshot_id=old_snapshot_id,
                    model_family=MODEL_FAMILY,
                    parameters_before={},
                    parameters_candidate=old_candidate,
                    training_metrics={},
                    validation_metrics={
                        "promotion_gate": {
                            "version": "stage5-personalization-gate.v2",
                            "passed": True,
                            "formal_promotion_eligible": True,
                        },
                        "formal_replay_audit": {
                            "engine": "stage5-real-ctssm-rolling-replay.v1"
                        },
                    },
                    sample_count=57,
                    status="promoted",
                ),
                LearnedModelProfile(
                    participant_id=person.id,
                    version=1,
                    parameters_json={
                        "S_star_init": 40.0,
                        "model_selection": {"active_variant": "m0"},
                    },
                    uncertainty_json={},
                    source="stage4-valid-m0",
                    model_version="mindflow-ctssm-runtime-v8",
                    validation_status="validated",
                    sample_count=30,
                    day_count=14,
                    confidence=0.8,
                    window_start=date(2029, 12, 1),
                    window_end=date(2029, 12, 31),
                ),
                LearnedModelProfile(
                    participant_id=person.id,
                    version=2,
                    parameters_json={
                        **old_candidate,
                        "model_selection": {
                            "status": "stage5_promoted",
                            "parameter_learning_run_id": str(old_run_id),
                            "active_variant": "m1",
                        },
                    },
                    uncertainty_json={},
                    source="stage5-old-promoted",
                    model_version="mindflow-ctssm-runtime-v10",
                    validation_status="validated",
                    sample_count=57,
                    day_count=19,
                    confidence=0.9,
                    window_start=date(2030, 1, 1),
                    window_end=date(2030, 1, 19),
                ),
            ]
        )

    repository = LearnedProfileRepository(database)
    with database.session() as session:
        old_profile = session.execute(
            __import__("sqlalchemy").select(LearnedModelProfile).where(
                LearnedModelProfile.participant_id == person.id,
                LearnedModelProfile.version == 2,
            )
        ).scalar_one()
        old_valid, old_evidence = repository.runtime_validity_in_session(
            session, old_profile
        )
    assert old_valid is False
    assert old_evidence["checks"]["profile_runtime_v11"] is False
    assert old_evidence["checks"]["promotion_gate_v3"] is False
    assert old_evidence["checks"]["formal_replay_v2"] is False
    assert old_evidence["checks"]["causal_dataset_schema"] is False
    fallback = repository.runtime_active(person.id)
    assert fallback is not None
    assert fallback["version"] == 1
    assert fallback["parameters"]["model_selection"]["active_variant"] == "m0"

    valid_snapshot_id = uuid.uuid4()
    valid_run_id = uuid.uuid4()
    valid_candidate = {"S_star_init": 55.0}
    with database.session() as session:
        session.add_all(
            [
                DatasetSnapshot(
                    id=valid_snapshot_id,
                    date_start=date(2030, 2, 1),
                    date_end=date(2030, 2, 19),
                    participant_filter={},
                    observation_cutoff=datetime(
                        2030, 2, 20, tzinfo=timezone.utc
                    ),
                    calendar_cutoff=datetime(
                        2030, 2, 20, tzinfo=timezone.utc
                    ),
                    schema_version=DATASET_SCHEMA_V6,
                    manifest_json={},
                ),
                ParameterLearningRun(
                    id=valid_run_id,
                    participant_id=person.id,
                    dataset_snapshot_id=valid_snapshot_id,
                    model_family=MODEL_FAMILY,
                    parameters_before={},
                    parameters_candidate=valid_candidate,
                    training_metrics={},
                    validation_metrics={
                        "promotion_gate": {
                            "version": PROMOTION_GATE_VERSION,
                            "passed": True,
                            "formal_promotion_eligible": True,
                        },
                        "formal_replay_audit": {
                            "engine": "stage5-real-ctssm-rolling-replay.v2"
                        },
                    },
                    sample_count=57,
                    status="promoted",
                ),
                LearnedModelProfile(
                    participant_id=person.id,
                    version=3,
                    parameters_json={
                        **valid_candidate,
                        "model_selection": {
                            "status": "stage5_promoted",
                            "parameter_learning_run_id": str(valid_run_id),
                            "active_variant": "m1",
                        },
                    },
                    uncertainty_json={},
                    source="stage5-valid-promoted",
                    model_version="mindflow-ctssm-runtime-v11",
                    validation_status="validated",
                    sample_count=57,
                    day_count=19,
                    confidence=0.9,
                    window_start=date(2030, 2, 1),
                    window_end=date(2030, 2, 19),
                ),
            ]
        )

    active = repository.runtime_active(person.id)
    assert active is not None
    assert active["version"] == 1
    with database.session() as session:
        v6_profile = session.execute(
            __import__("sqlalchemy").select(LearnedModelProfile).where(
                LearnedModelProfile.participant_id == person.id,
                LearnedModelProfile.version == 3,
            )
        ).scalar_one()
        valid, evidence = repository.runtime_validity_in_session(
            session, v6_profile
        )
    assert valid is False
    assert evidence["checks"]["causal_dataset_schema"] is False

    v7_snapshot_id = uuid.uuid4()
    v7_run_id = uuid.uuid4()
    v7_snapshot_identity = {
        "profile_id": "snapshot-active-m1",
        "profile_version": 7,
        "parameters_hash": "snapshot-active-m1-hash",
        "active_variant": "m1",
        "model_spec_version": "stress-ctssm-model-spec.v1:m1",
        "stage4_promotion_decision_id": "stage4-decision",
        "stage4_status": "promoted",
    }
    v7_family_evidence = {
        "passed": True,
        "snapshot_cutoff": datetime(
            2030, 3, 20, tzinfo=timezone.utc
        ).isoformat(),
        "snapshot_cutoff_active_identity": v7_snapshot_identity,
    }
    v7_effective_evidence = _validated_effective_profile(
        valid_candidate, variant="m1"
    )
    v7_effective_evidence["snapshot_cutoff"] = datetime(
        2030, 3, 20, tzinfo=timezone.utc
    ).isoformat()
    v7_selection = {
        "workflow": "stage5_candidate_active",
        "status": "stage5_promoted",
        "parameter_learning_run_id": str(v7_run_id),
        "dataset_snapshot_id": str(v7_snapshot_id),
        "promotion_gate_version": PROMOTION_GATE_VERSION,
        "active_variant": "m1",
        "model_spec_version": "stress-ctssm-model-spec.v1:m1",
        "validated_effective_parameters_hash": v7_effective_evidence[
            "validated_effective_parameters_hash"
        ],
        "explicit_profile_identity": v7_effective_evidence[
            "explicit_profile_identity"
        ],
    }
    with database.session() as session:
        session.add_all(
            [
                DatasetSnapshot(
                    id=v7_snapshot_id,
                    date_start=date(2030, 3, 1),
                    date_end=date(2030, 3, 19),
                    participant_filter={},
                    observation_cutoff=datetime(
                        2030, 3, 20, tzinfo=timezone.utc
                    ),
                    calendar_cutoff=datetime(
                        2030, 3, 20, tzinfo=timezone.utc
                    ),
                    schema_version=DATASET_SCHEMA_V7,
                    manifest_json={},
                ),
                ParameterLearningRun(
                    id=v7_run_id,
                    participant_id=person.id,
                    dataset_snapshot_id=v7_snapshot_id,
                    model_family=MODEL_FAMILY,
                    parameters_before={},
                    parameters_candidate=valid_candidate,
                    training_metrics={
                        "deployment_family_evidence": v7_family_evidence,
                        "validated_effective_profile": v7_effective_evidence,
                    },
                    validation_metrics={
                        "promotion_gate": {
                            "version": PROMOTION_GATE_VERSION,
                            "passed": True,
                            "formal_promotion_eligible": True,
                        },
                        "formal_replay_audit": {
                            "engine": "stage5-real-ctssm-rolling-replay.v2"
                        },
                        "deployment_family_gate": v7_family_evidence,
                        "validated_effective_profile": v7_effective_evidence,
                    },
                    sample_count=57,
                    status="promoted",
                ),
                LearnedModelProfile(
                    participant_id=person.id,
                    version=4,
                    parameters_json={
                        **valid_candidate,
                        "model_selection": v7_selection,
                    },
                    uncertainty_json={},
                    source="stage5-v7-promoted",
                    model_version="mindflow-ctssm-runtime-v11",
                    validation_status="validated",
                    sample_count=57,
                    day_count=19,
                    confidence=0.9,
                    window_start=date(2030, 3, 1),
                    window_end=date(2030, 3, 19),
                ),
            ]
        )

    active = repository.runtime_active(person.id)
    assert active is not None
    assert active["version"] == 4
    assert active["runtime_validation"]["runtime_valid"] is True
    assert active["runtime_validation"]["provenance_type"] == (
        "stage5_promotion"
    )


def test_stage5_effective_profile_authorization_matches_oot_and_fails_closed_on_drift():
    database = memory_database()
    person = participant(database, "stage5-explicit-runtime")
    snapshot_id = uuid.uuid4()
    run_id = uuid.uuid4()
    explicit_id = uuid.uuid4()
    explicit_created_at = datetime(2030, 1, 10, tzinfo=timezone.utc)
    cutoff = datetime(2030, 1, 20, tzinfo=timezone.utc)
    explicit_parameters = {
        "S_star_init": 58.0,
        "ctssm_params": {"workload_stress_gain": 17.0},
    }
    candidate = {
        "S_star_init": 52.0,
        "ctssm_params": {
            "workload_stress_gain": 12.0,
            "recovery_stress_gain": 8.0,
        },
    }
    explicit_identity = {
        "profile_id": str(explicit_id),
        "version": 1,
        "created_at": explicit_created_at.isoformat(),
        "parameters_hash": profile_parameters_hash(explicit_parameters),
        "source": "participant_profiles",
    }
    validated_hash = stage5_effective_parameters_hash(
        _production_current_parameters(candidate, explicit_parameters), "m1"
    )
    family_identity = {
        "profile_id": "snapshot-stage4-m1",
        "profile_version": 5,
        "parameters_hash": "snapshot-stage4-m1-hash",
        "active_variant": "m1",
        "model_spec_version": "stress-ctssm-model-spec.v1:m1",
        "stage4_promotion_decision_id": "stage4-m1-decision",
        "stage4_status": "retained_from_empirical_evidence",
    }
    family_evidence = {
        "passed": True,
        "snapshot_cutoff": cutoff.isoformat(),
        "snapshot_cutoff_active_identity": family_identity,
    }
    effective_evidence = {
        "version": "stage5-effective-profile-provenance.v1",
        "passed": True,
        "snapshot_cutoff": cutoff.isoformat(),
        "explicit_profile_identity": explicit_identity,
        "explicit_profile_provenance": {
            **explicit_identity,
            "origin_cutoff": cutoff.isoformat(),
            "knowledge_cutoff": cutoff.isoformat(),
            "cutoff_operator": "<=",
            "snapshot_item_type": "participant_profile",
            "usage": "deployment_explicit_profile",
        },
        "validated_effective_parameters_hash": validated_hash,
    }
    selection = {
        "workflow": "stage5_candidate_active",
        "status": "stage5_promoted",
        "parameter_learning_run_id": str(run_id),
        "dataset_snapshot_id": str(snapshot_id),
        "promotion_gate_version": PROMOTION_GATE_VERSION,
        "active_variant": "m1",
        "model_spec_version": "stress-ctssm-model-spec.v1:m1",
        "validated_effective_parameters_hash": validated_hash,
        "parameters_hash": validated_hash,
        "explicit_profile_identity": explicit_identity,
    }
    with database.session() as session:
        session.add(
            DatasetSnapshot(
                id=snapshot_id,
                date_start=date(2030, 1, 1),
                date_end=date(2030, 1, 19),
                participant_filter={},
                observation_cutoff=cutoff,
                calendar_cutoff=cutoff,
                schema_version=DATASET_SCHEMA_V7,
                manifest_json={},
            )
        )
        session.add(
            ParticipantProfile(
                id=explicit_id,
                participant_id=person.id,
                version=1,
                profile_json={"model_params": explicit_parameters},
                created_at=explicit_created_at,
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
                training_metrics={
                    "deployment_family_evidence": family_evidence,
                    "validated_effective_profile": effective_evidence,
                },
                validation_metrics={
                    "promotion_gate": {
                        "version": PROMOTION_GATE_VERSION,
                        "passed": True,
                        "formal_promotion_eligible": True,
                    },
                    "formal_replay_audit": {
                        "engine": "stage5-real-ctssm-rolling-replay.v2"
                    },
                    "deployment_family_gate": family_evidence,
                    "validated_effective_profile": effective_evidence,
                },
                sample_count=57,
                status="promoted",
            )
        )
        session.add_all(
            [
                LearnedModelProfile(
                    participant_id=person.id,
                    version=1,
                    parameters_json={
                        "S_star_init": 40.0,
                        "model_selection": {"active_variant": "m0"},
                    },
                    uncertainty_json={},
                    source="fallback-m0",
                    model_version="mindflow-ctssm-runtime-v8",
                    validation_status="validated",
                    sample_count=30,
                    day_count=14,
                    confidence=0.8,
                    window_start=date(2029, 12, 1),
                    window_end=date(2029, 12, 31),
                ),
                LearnedModelProfile(
                    participant_id=person.id,
                    version=2,
                    parameters_json={**candidate, "model_selection": selection},
                    uncertainty_json={},
                    source="stage5-effective-promoted",
                    model_version="mindflow-ctssm-runtime-v11",
                    validation_status="validated",
                    sample_count=57,
                    day_count=19,
                    confidence=0.9,
                    window_start=date(2030, 1, 1),
                    window_end=date(2030, 1, 19),
                ),
            ]
        )

    learned = LearnedProfileRepository(database).runtime_active(person.id)
    assert learned is not None and learned["version"] == 2
    explicit = ProfileRepository(database).current(person.id)
    effective, _layers = layered_profile(explicit, learned)
    authorized = enforce_promoted_model_selection(effective, learned)
    assert authorized["model_params"]["model_selection"][
        "active_variant"
    ] == "m1"
    oot_parameters = _production_current_parameters(
        candidate, explicit_parameters
    )
    assert stage5_effective_parameters_hash(
        authorized["model_params"], "m1"
    ) == (
        stage5_effective_parameters_hash(oot_parameters, "m1")
    )
    model = AssessmentModel("Asia/Shanghai")
    production = model.predict(
        profile=authorized,
        observations=[],
        calendar_events=[],
        local_date="2030-01-21",
    )
    oot = model.predict_candidate(
        model_variant="m1",
        candidate_params=oot_parameters,
        observations=[],
        calendar_events=[],
        local_date="2030-01-21",
    )
    assert production.model_variant == oot.model_variant == "m1"
    assert production.trajectory == oot.trajectory

    with database.session() as session:
        promoted = session.execute(
            __import__("sqlalchemy").select(LearnedModelProfile).where(
                LearnedModelProfile.participant_id == person.id,
                LearnedModelProfile.version == 2,
            )
        ).scalar_one()
        promoted.parameters_json = {
            **candidate,
            "model_selection": {**selection, "active_variant": "m3"},
        }
    assert LearnedProfileRepository(database).runtime_active(person.id)[
        "version"
    ] == 1

    with database.session() as session:
        promoted = session.execute(
            __import__("sqlalchemy").select(LearnedModelProfile).where(
                LearnedModelProfile.participant_id == person.id,
                LearnedModelProfile.version == 2,
            )
        ).scalar_one()
        promoted.parameters_json = {**candidate, "model_selection": selection}
        session.add(
            ParticipantProfile(
                participant_id=person.id,
                version=2,
                profile_json={
                    "model_params": {**explicit_parameters, "S_star_init": 63.0}
                },
            )
        )
    assert LearnedProfileRepository(database).runtime_active(person.id)[
        "version"
    ] == 1


def test_training_service_persists_run_and_candidate_profile_from_snapshot(monkeypatch):
    database = memory_database()
    person = participant(database, "stage5-training")
    snapshot_id = uuid.uuid4()
    cutoff = datetime(2030, 1, 20, tzinfo=timezone.utc)
    snapshot_row = DatasetSnapshot(
                id=snapshot_id,
                date_start=date(2030, 1, 1),
                date_end=date(2030, 1, 19),
                participant_filter={"participant_codes": ["stage5-training"]},
                observation_cutoff=datetime(2030, 1, 20, tzinfo=timezone.utc),
                calendar_cutoff=datetime(2030, 1, 20, tzinfo=timezone.utc),
                schema_version=DATASET_SCHEMA_V7,
                manifest_json={},
            )
    item_row = DatasetSnapshotItem(
                dataset_snapshot_id=snapshot_id,
                item_type="participant",
                source_id=str(person.id),
                source_version="participant-membership.v1",
                participant_id=person.id,
                local_date=date(2030, 1, 1),
                source_hash="frozen-participant",
                    metadata_json={
                        "participant_id": str(person.id),
                        "participant_code": "stage5-training",
                    },
            )
    item_view = DatasetSnapshotIntegrityService.item_view(item_row)
    contract = {
        "schema_version": DATASET_SCHEMA_V7,
        "date_start": "2030-01-01",
        "date_end": "2030-01-19",
        "participant_filter": {"participant_codes": ["stage5-training"]},
        "observation_cutoff": cutoff.isoformat(),
        "calendar_cutoff": cutoff.isoformat(),
    }
    snapshot_row.manifest_json = {
        "schema_version": DATASET_SCHEMA_V7,
        "participant_count": 1,
        "observation_count": 0,
        "forecast_count": 0,
        "calendar_count": 0,
        "psychometric_count": 0,
        "daily_review_count": 0,
        "slow_state_count": 0,
        "care_intervention_exposure_count": 0,
        "warning_delivery_count": 0,
        "participant_profile_count": 0,
        "learned_model_profile_count": 0,
        "item_count": 1,
        "manifest_hash": DatasetSnapshotIntegrityService.manifest_hash(
            contract, [item_view]
        ),
    }
    with database.session() as session:
        session.add(snapshot_row)
        session.add(item_row)
        live_after_cutoff = LearnedModelProfile(
            participant_id=person.id,
            version=1,
            parameters_json={
                "S_star_init": 88.0,
                "model_selection": {"active_variant": "m2"},
            },
            uncertainty_json={},
            source="live-after-snapshot-cutoff",
            model_version="mindflow-ctssm-runtime-v8",
            validation_status="validated",
            sample_count=30,
            day_count=14,
            confidence=0.8,
            window_start=date(2030, 1, 1),
            window_end=date(2030, 1, 19),
            created_at=datetime(2030, 1, 25, tzinfo=timezone.utc),
        )
        session.add(live_after_cutoff)
        session.flush()
        live_after_cutoff_id = str(live_after_cutoff.id)
    service = ParameterLearningService(database, "Asia/Shanghai")
    live_after_cutoff_view = LearnedProfileRepository(database).latest(
        person.id
    )
    monkeypatch.setattr(
        service.learned_profiles,
        "runtime_active",
        lambda participant_id: live_after_cutoff_view,
    )
    frozen = _samples(days=19, participant_id=str(person.id))
    monkeypatch.setattr(
        service.extractor,
        "_extract",
        lambda items, participant_id: {
            "samples": frozen,
            "calendars": {},
            "observation_history": {},
        },
    )
    validation_calls = []

    def formal_validate(**kwargs):
        validation_calls.append(kwargs)
        return {
            "rolling_origin": {"split_count": 5},
            "comparison": {},
            "promotion_gate": {"passed": False},
            "residual_gate": {"passed": False},
            "formal_replay_audit": {"surrogate_predict_base_used": False},
            "latest_residual_model": {
                "mode": "shadow",
                "sample_count": 40,
                "residual_sd": 0.5,
            },
        }

    monkeypatch.setattr(
        service.formal_replay,
        "validate",
        formal_validate,
    )

    result = service.train_snapshot(snapshot_id, person.id)

    assert result["sample_count"] == 57
    assert result["status"] in {"candidate", "rejected"}
    assert result["training_metrics"]["minimum_data_gate"]["passed"] is True
    assert result["validation_metrics"]["rolling_origin"]["split_count"] == 5
    assert not any(
        item["item_type"] == "learned_model_profile"
        for item in validation_calls[0]["items"]
    )
    assert result["training_metrics"]["base_active_identity"][
        "profile_id"
    ] == live_after_cutoff_id
    assert result["training_metrics"][
        "deployment_base_active_identity_at_train_time"
    ]["usage"] == "deployment_base_active_identity_at_train_time"
    assert result["status"] == "rejected"
    assert result["validation_metrics"]["deployment_family_gate"][
        "passed"
    ] is False
    assert result["validation_metrics"]["deployment_family_gate"][
        "reason"
    ] == "active_changed_after_snapshot_cutoff_require_resnapshot"
    latest = LearnedProfileRepository(database).latest(person.id)
    assert latest["validation_status"] == result["status"]
    assert latest["parameters"]["model_selection"]["active_variant"] == "m0"
    assert latest["parameters"]["model_selection"][
        "parameter_learning_run_id"
    ] == result["id"]

    with database.session() as session:
        frozen_item = session.execute(
            __import__("sqlalchemy").select(DatasetSnapshotItem).where(
                DatasetSnapshotItem.dataset_snapshot_id == snapshot_id
            )
        ).scalar_one()
        frozen_item.source_hash = "tampered-after-freeze"
    with pytest.raises(ValueError, match="manifest mismatch"):
        service._snapshot(snapshot_id, person.id)


def test_candidate_family_comes_from_snapshot_cutoff_active(monkeypatch):
    database = memory_database()
    person = participant(database, "stage5-snapshot-family")
    snapshot_id = uuid.uuid4()
    profile_id = uuid.uuid4()
    cutoff = datetime(2030, 1, 20, tzinfo=timezone.utc)
    frozen_parameters = {
        "S_star_init": 48.0,
        "model_selection": {"active_variant": "m1"},
    }
    participant_item = DatasetSnapshotItem(
        dataset_snapshot_id=snapshot_id,
        item_type="participant",
        source_id=str(person.id),
        source_version="participant-membership.v1",
        participant_id=person.id,
        local_date=date(2030, 1, 1),
        source_hash="participant-source-hash",
        metadata_json={
            "participant_id": str(person.id),
            "participant_code": person.participant_code,
        },
    )
    learned_item = DatasetSnapshotItem(
        dataset_snapshot_id=snapshot_id,
        item_type="learned_model_profile",
        source_id=str(profile_id),
        source_version="learned-model-runtime-identity.v1",
        participant_id=person.id,
        local_date=date(2030, 1, 19),
        source_hash="learned-source-hash",
        metadata_json={
            "profile_id": str(profile_id),
            "version": 1,
            "parameters": frozen_parameters,
            "model_selection": frozen_parameters["model_selection"],
            "parameters_hash": promotion_parameters_hash(frozen_parameters),
            "model_version": "mindflow-ctssm-runtime-v11",
            "validation_status": "validated",
            "source": "frozen-active",
            "created_at": "2030-01-19T08:00:00+00:00",
            "active_variant": "m1",
            "runtime_valid": True,
            "runtime_validation": {
                "runtime_valid": True,
                "provenance_type": "stage5_promotion",
            },
        },
    )
    item_views = [
        DatasetSnapshotIntegrityService.item_view(participant_item),
        DatasetSnapshotIntegrityService.item_view(learned_item),
    ]
    contract = {
        "schema_version": DATASET_SCHEMA_V7,
        "date_start": "2030-01-01",
        "date_end": "2030-01-19",
        "participant_filter": {
            "participant_codes": [person.participant_code]
        },
        "observation_cutoff": cutoff.isoformat(),
        "calendar_cutoff": cutoff.isoformat(),
    }
    manifest = {
        "schema_version": DATASET_SCHEMA_V7,
        "participant_count": 1,
        "observation_count": 0,
        "forecast_count": 0,
        "calendar_count": 0,
        "psychometric_count": 0,
        "daily_review_count": 0,
        "slow_state_count": 0,
        "care_intervention_exposure_count": 0,
        "warning_delivery_count": 0,
        "participant_profile_count": 0,
        "learned_model_profile_count": 1,
        "item_count": 2,
        "manifest_hash": DatasetSnapshotIntegrityService.manifest_hash(
            contract, item_views
        ),
    }
    with database.session() as session:
        session.add(
            DatasetSnapshot(
                id=snapshot_id,
                date_start=date(2030, 1, 1),
                date_end=date(2030, 1, 19),
                participant_filter=contract["participant_filter"],
                observation_cutoff=cutoff,
                calendar_cutoff=cutoff,
                schema_version=DATASET_SCHEMA_V7,
                manifest_json=manifest,
            )
        )
        session.add_all([participant_item, learned_item])

    service = ParameterLearningService(database, "Asia/Shanghai")
    live_view = {
        "id": str(profile_id),
        "version": 1,
        "parameters": frozen_parameters,
    }
    monkeypatch.setattr(
        service.learned_profiles, "runtime_active", lambda participant_id: live_view
    )
    samples = _samples(days=19, participant_id=str(person.id))
    monkeypatch.setattr(
        service.extractor,
        "_extract",
        lambda items, participant_id: {
            "samples": samples,
            "calendars": {},
            "observation_history": {},
        },
    )
    monkeypatch.setattr(
        service.formal_replay,
        "validate",
        lambda **kwargs: {
            "rolling_origin": {"split_count": 5},
            "comparison": {},
            "promotion_gate": {"passed": True},
            "residual_gate": {"passed": False},
            "formal_replay_audit": {"surrogate_predict_base_used": False},
            "latest_residual_model": {
                "mode": "shadow",
                "sample_count": 40,
                "residual_sd": 0.5,
            },
        },
    )

    result = service.train_snapshot(snapshot_id, person.id)
    candidate_profile = LearnedProfileRepository(database).latest(person.id)

    assert result["status"] == "candidate"
    assert result["validation_metrics"]["deployment_family_gate"][
        "passed"
    ] is True
    assert candidate_profile["parameters"]["model_selection"][
        "active_variant"
    ] == "m1"
    assert candidate_profile["parameters"]["model_selection"][
        "model_spec_version"
    ].endswith(":m1")


def test_promotion_rejects_candidate_when_base_active_profile_has_changed():
    database = memory_database()
    person = participant(database, "stage5-stale")
    service = ParameterLearningService(database, "Asia/Shanghai")
    snapshot_id = uuid.uuid4()
    run_id = uuid.uuid4()
    fitted = fit_partial_pooling(_samples(), _samples())
    candidate = runtime_candidate_parameters(fitted)
    candidate["residual_model"] = {"mode": "shadow", "residual_sd": 0.4}
    uncertainty = {
        "hierarchical_parameters": fitted["uncertainty"],
        "S_star_init": {"std_error": 1.0},
        "ctssm_params": {"std_error": 1.0},
        "hierarchical_population_prior": {"std_error": 1.0},
        "residual_model": {"std_error": 0.4},
    }
    deployment_identity = service._active_identity(None)
    deployment_family_evidence = {
        "passed": True,
        "reason": "snapshot_cutoff_active_matches_train_time_live",
        "snapshot_cutoff": datetime(
            2030, 1, 20, tzinfo=timezone.utc
        ).isoformat(),
        "snapshot_cutoff_active_identity": deployment_identity,
        "train_time_live_active_identity": deployment_identity,
    }
    effective_profile_evidence = _validated_effective_profile(candidate)
    with database.session() as session:
        session.add_all(_valid_v7_snapshot_rows(snapshot_id, person))
        session.add(
            ParameterLearningRun(
                id=run_id,
                participant_id=person.id,
                dataset_snapshot_id=snapshot_id,
                model_family=MODEL_FAMILY,
                parameters_before={},
                parameters_candidate=candidate,
                training_metrics={
                    "minimum_data_gate": {
                        "passed": True,
                        "counts": {"observed_days": 19},
                    },
                    "base_active_identity": service._active_identity(None),
                    "deployment_family_evidence": deployment_family_evidence,
                    "validated_effective_profile": effective_profile_evidence,
                },
                validation_metrics={
                    "promotion_gate": {
                        "passed": True,
                        "formal_promotion_eligible": True,
                        "version": PROMOTION_GATE_VERSION,
                    },
                    "formal_replay_audit": {
                        "engine": service.formal_replay.FORMAL_ENGINE_VERSION
                    },
                    "uncertainty": uncertainty,
                    "deployment_family_gate": deployment_family_evidence,
                    "validated_effective_profile": effective_profile_evidence,
                },
                sample_count=57,
                status="candidate",
            )
        )
        session.add(
            LearnedModelProfile(
                participant_id=person.id,
                version=1,
                parameters_json={
                    **candidate,
                    "model_selection": {
                        "status": "stage5_candidate",
                        "workflow": "stage5_candidate_active",
                        "parameter_learning_run_id": str(run_id),
                        "dataset_snapshot_id": str(snapshot_id),
                        "promotion_gate_version": PROMOTION_GATE_VERSION,
                        "active_variant": deployment_identity[
                            "active_variant"
                        ],
                        "model_spec_version": deployment_identity[
                            "model_spec_version"
                        ],
                        "validated_effective_parameters_hash": (
                            effective_profile_evidence[
                                "validated_effective_parameters_hash"
                            ]
                        ),
                        "explicit_profile_identity": (
                            effective_profile_evidence[
                                "explicit_profile_identity"
                            ]
                        ),
                    },
                },
                uncertainty_json=uncertainty,
                source=LEARNING_VERSION,
                model_version="mindflow-ctssm-runtime-v10",
                validation_status="candidate",
                sample_count=57,
                day_count=19,
                confidence=0.9,
                window_start=date(2030, 1, 1),
                window_end=date(2030, 1, 19),
            )
        )
        session.add(
            LearnedModelProfile(
                participant_id=person.id,
                version=2,
                parameters_json={
                    "S_star_init": 55.0,
                    "model_selection": {"active_variant": "m0"},
                },
                uncertainty_json={"S_star_init": {"std_error": 1.0}},
                source="concurrent-update",
                model_version="mindflow-ctssm-runtime-v10",
                validation_status="validated",
                sample_count=40,
                day_count=15,
                confidence=0.8,
                window_start=date(2030, 1, 1),
                window_end=date(2030, 1, 15),
            )
        )

    with pytest.raises(ValueError, match="stale_parameter_learning_candidate"):
        service.promote(run_id)

    with database.session() as session:
        run = session.get(ParameterLearningRun, run_id)
        assert run.status == "candidate"
        profiles = session.execute(
            __import__("sqlalchemy").select(LearnedModelProfile).where(
                LearnedModelProfile.participant_id == person.id
            )
        ).scalars().all()
        assert len(profiles) == 2


def test_promotion_requires_resnapshot_when_explicit_profile_changed_after_cutoff():
    database = memory_database()
    person = participant(database, "stage5-explicit-promotion-cas")
    service, run_id, _snapshot_id = _seed_promotable_m0_candidate(
        database, person
    )
    with database.session() as session:
        session.add(
            ParticipantProfile(
                participant_id=person.id,
                version=1,
                profile_json={"model_params": {"S_star_init": 61.0}},
            )
        )

    with pytest.raises(
        ValueError,
        match=(
            "explicit_profile_changed_after_snapshot_cutoff_require_resnapshot"
        ),
    ):
        service.promote(run_id)

    with database.session() as session:
        assert session.get(ParameterLearningRun, run_id).status == "candidate"


def test_promotion_rechecks_immutable_dataset_snapshot_integrity():
    database = memory_database()
    person = participant(database, "stage5-promotion-integrity")
    service, run_id, snapshot_id = _seed_promotable_m0_candidate(
        database, person
    )
    with database.session() as session:
        item = session.execute(
            __import__("sqlalchemy").select(DatasetSnapshotItem).where(
                DatasetSnapshotItem.dataset_snapshot_id == snapshot_id
            )
        ).scalar_one()
        item.source_hash = "tampered-after-training"

    with pytest.raises(
        ValueError, match="dataset_snapshot_integrity_mismatch"
    ):
        service.promote(run_id)

    with database.session() as session:
        assert session.get(ParameterLearningRun, run_id).status == "candidate"


def test_scheduled_week_has_database_level_idempotency_constraint():
    database = memory_database()
    person = participant(database, "stage5-scheduled-unique")
    snapshot_id = uuid.uuid4()
    with database.session() as session:
        session.add(
            DatasetSnapshot(
                id=snapshot_id,
                date_start=date(2030, 1, 1),
                date_end=date(2030, 1, 7),
                participant_filter={},
                observation_cutoff=datetime(2030, 1, 8, tzinfo=timezone.utc),
                calendar_cutoff=datetime(2030, 1, 8, tzinfo=timezone.utc),
                schema_version=DATASET_SCHEMA_V7,
                manifest_json={},
            )
        )
        session.add(
            ParameterLearningRun(
                participant_id=person.id,
                dataset_snapshot_id=snapshot_id,
                model_family=MODEL_FAMILY,
                run_kind="scheduled",
                schedule_key="2030-W01",
                parameters_before={},
                parameters_candidate={},
                training_metrics={},
                validation_metrics={},
                sample_count=0,
                status="rejected",
            )
        )

    with pytest.raises(IntegrityError):
        with database.session() as session:
            session.add(
                ParameterLearningRun(
                    participant_id=person.id,
                    dataset_snapshot_id=snapshot_id,
                    model_family=MODEL_FAMILY,
                    run_kind="scheduled",
                    schedule_key="2030-W01",
                    parameters_before={},
                    parameters_candidate={},
                    training_metrics={},
                    validation_metrics={},
                    sample_count=0,
                    status="rejected",
                )
            )


def test_weekly_calibration_does_not_reuse_same_date_manual_snapshot(monkeypatch):
    database = memory_database()
    person = participant(database, "stage5-weekly-batch")
    through = date(2030, 2, 24)
    date_start = through.fromordinal(
        through.toordinal() - ParameterLearningService.SNAPSHOT_WINDOW_DAYS + 1
    )
    research = ResearchEvaluationService(database, "Asia/Shanghai")
    manual = research.create_dataset_snapshot(
        date_start=date_start,
        date_end=through,
        participant_filter={},
    )
    service = ParameterLearningService(database, "Asia/Shanghai")
    captured = {}

    def fake_train(snapshot_id, participant_id, **kwargs):
        captured.update(
            {
                "snapshot_id": str(snapshot_id),
                "participant_id": str(participant_id),
                **kwargs,
            }
        )
        return {"status": "rejected", "id": str(uuid.uuid4())}

    monkeypatch.setattr(service, "train_snapshot", fake_train)
    result = service.maybe_calibrate(person.id, through=through)

    assert captured["snapshot_id"] != manual["id"]
    assert captured["run_kind"] == "scheduled"
    assert captured["schedule_key"] == "2030-W08"
    assert result["snapshot"]["purpose"] == "stage5_weekly_calibration"
    assert result["snapshot"]["schedule_key"] == "2030-W08"
    with database.session() as session:
        snapshots = session.execute(
            __import__("sqlalchemy").select(DatasetSnapshot).order_by(
                DatasetSnapshot.created_at
            )
        ).scalars().all()
        assert len(snapshots) == 2
        assert snapshots[0].purpose == "manual_research"
        assert snapshots[1].purpose == "stage5_weekly_calibration"


def test_unrelated_scheduled_integrity_error_is_rethrown_unchanged():
    database = memory_database()
    service = ParameterLearningService(database, "Asia/Shanghai")
    error = IntegrityError(
        "INSERT unrelated constraint",
        {},
        RuntimeError("different database constraint"),
    )

    with pytest.raises(IntegrityError) as caught:
        service._resolve_scheduled_integrity_error(
            error,
            participant_id=uuid.uuid4(),
            schedule_key="2030-W01",
        )

    assert caught.value is error
