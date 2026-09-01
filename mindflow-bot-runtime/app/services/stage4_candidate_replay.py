"""Frozen rolling-origin replay through the real CTSSM implementation."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, time, timedelta, timezone
from typing import Any, Mapping, Sequence

from algorithm.dynamic_state_model import normalize_model_variant
from app.services.model_comparison import (
    MODEL_FAMILIES,
    MODEL_VARIANT_BY_FAMILY,
    ROLLING_ORIGIN_VERSION,
    comparison_metrics,
    estimate_response_rates,
    fit_workload_candidate_parameters,
    observed_recovery_efficiency,
    promotion_gate,
    rolling_origin_splits,
    trait_resilience_prior,
)
from entry.config import GLOBAL_DEFAULT_CONFIG
from mindflow_core.assessment import AssessmentModel


OBSERVABLE_SUPPORT_CONFIG = dict(GLOBAL_DEFAULT_CONFIG["observable_support"])
REPLAY_ENGINE_VERSION = "stage4-real-ctssm-replay.v5"
CANDIDATE_LATENT_INITIALIZATION_VERSION = "candidate-latent-initialization.v1"
DEPLOYMENT_REFIT_VERSION = "stage4-deployment-refit.v1"
M0_SIMULATOR_FIT_VERSION = "m0-simulator-fit.v2"


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result


def _aware(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return (
        parsed.replace(tzinfo=timezone.utc)
        if parsed.tzinfo is None
        else parsed.astimezone(timezone.utc)
    )


def _trajectory_peak(trajectory: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    point = max(
        trajectory,
        key=lambda item: float(item.get("stress_0_10") or 0.0),
        default=None,
    )
    return {
        "trajectory_peak_stress": (
            float(point.get("stress_0_10") or 0.0) if point else None
        ),
        "trajectory_peak_time": str(point.get("time") or "")[:5] if point else None,
    }


def _candidate_parameters(fitted: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "S_star_init": float(fitted["stress_baseline_0_10"]) * 10.0,
        "ctssm_params": {
            "workload_stress_gain": (
                float(fitted["workload_reactivity_beta"]) * 10.0
            ),
            "recovery_stress_gain": float(fitted["recovery_beta"]) * 10.0,
        },
    }


def _candidate_uncertainty(fitted: Mapping[str, Any]) -> dict[str, Any]:
    source = dict(fitted.get("uncertainty") or {})

    def scaled(name: str) -> dict[str, float | None]:
        value = _number((source.get(name) or {}).get("std_error"))
        return {
            "std_error": round(value * 10.0, 6) if value is not None else None
        }

    return {
        "S_star_init": scaled("stress_baseline_0_10"),
        "ctssm_params": {
            "workload_stress_gain": scaled("workload_reactivity_beta"),
            "recovery_stress_gain": scaled("recovery_beta"),
        },
        "estimation": {
            "fit_method": fitted.get("fit_method"),
            "parameter_fit_version": fitted.get("parameter_fit_version"),
            "uncertainty_method": fitted.get("uncertainty_method"),
            "ridge_lambda": fitted.get("ridge_lambda"),
            "sample_count": fitted.get("sample_count"),
            "design_condition_number": fitted.get("design_condition_number"),
            "identifiability_status": fitted.get("identifiability_status"),
            "boundary_clipped": bool(fitted.get("boundary_clipped")),
        },
    }


def fit_current_m0_parameters_v2(
    training_samples: Sequence[Mapping[str, Any]],
    frozen_context: Mapping[str, Any],
    origin_cutoff: datetime,
    assessment_model: AssessmentModel,
) -> dict[str, Any]:
    """Fit S_star_init with bounded SSE searches through the real M0 simulator."""

    prepared = []
    for sample in training_samples:
        try:
            target = _aware(sample["observed_at"])
            created = _aware(sample["observation_created_at"])
            actual = _number(sample.get("actual_stress"))
        except (KeyError, TypeError, ValueError):
            continue
        if actual is None or target >= origin_cutoff or created >= origin_cutoff:
            continue
        participant = str(sample.get("participant_id") or "")
        calendar = frozen_context.get("calendars", {}).get(
            sample.get("forecast_id")
        ) or {}
        point_time = str(
            (sample.get("context") or {}).get("forecast_point_time") or ""
        )[:5]
        if not point_time:
            continue
        prepared.append(
            {
                "actual": actual,
                "point_time": point_time,
                "observations": Stage4CandidateReplayService._known_observations(
                    frozen_context.get("observation_history", {}).get(
                        participant, []
                    ),
                    target,
                ),
                "calendar_events": Stage4CandidateReplayService._calendar_events(
                    calendar
                ),
                "local_date": str(sample["local_date"]),
                "initial_state": Stage4CandidateReplayService._frozen_initial_state(
                    sample
                ),
                "sleep_debt_hours": float(sample.get("sleep_debt") or 0.0),
            }
        )

    loss_cache: dict[float, tuple[float, int]] = {}

    def objective(s_star_init: float) -> tuple[float, int]:
        key = round(max(0.0, min(100.0, s_star_init)), 1)
        if key in loss_cache:
            return loss_cache[key]
        squared_errors = []
        for context in prepared:
            result = assessment_model.predict_baseline_m0(
                baseline_params={"S_star_init": key},
                observations=context["observations"],
                calendar_events=context["calendar_events"],
                local_date=context["local_date"],
                initial_state=context["initial_state"],
                sleep_debt_hours=context["sleep_debt_hours"],
            )
            point = next(
                (
                    row
                    for row in result.trajectory
                    if str(row.get("time") or "")[:5] == context["point_time"]
                ),
                None,
            )
            if point is None:
                continue
            predicted = _number(point.get("stress_0_10"))
            if predicted is not None:
                squared_errors.append((context["actual"] - predicted) ** 2)
        value = (sum(squared_errors), len(squared_errors))
        loss_cache[key] = value
        return value

    coarse_values = [float(value) for value in range(0, 101, 2)]
    if not prepared:
        best_value, best_loss, fitted_count = 50.0, None, 0
    else:
        best_coarse = min(
            coarse_values,
            key=lambda value: (objective(value)[0], value),
        )
        lower = max(0, int(round((best_coarse - 2.0) * 10)))
        upper = min(1000, int(round((best_coarse + 2.0) * 10)))
        fine_values = [value / 10.0 for value in range(lower, upper + 1)]
        best_value = min(
            fine_values,
            key=lambda value: (objective(value)[0], value),
        )
        best_loss, fitted_count = objective(best_value)
    training_days = sorted({str(context["local_date"]) for context in prepared})
    return {
        "S_star_init": round(best_value, 1),
        "stress_baseline_0_10": round(best_value / 10.0, 4),
        "training_loss": round(best_loss, 8) if best_loss is not None else None,
        "sample_count": fitted_count,
        "day_count": len(training_days),
        "training_window_start": training_days[0] if training_days else None,
        "training_window_end": training_days[-1] if training_days else None,
        "fit_method": "simulator-restricted-sse",
        "parameter_fit_version": M0_SIMULATOR_FIT_VERSION,
        "origin_cutoff": origin_cutoff.isoformat(),
        "search": {
            "bounds": [0.0, 100.0],
            "coarse_step": 2.0,
            "fine_step": 0.1,
            "evaluated_parameter_count": len(loss_cache),
        },
    }


def aggregate_evaluation_parameter_gate_evidence(
    evidence_by_participant: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Aggregate only final rolling-training fits for the formal gate."""

    final_training_evidence = list(evidence_by_participant.values())
    statuses = {
        str(value.get("identifiability_status") or "not_identified")
        for value in final_training_evidence
    }
    return {
        "identifiability_status": (
            "not_identified"
            if not statuses or "not_identified" in statuses
            else "weak" if "weak" in statuses else "identified"
        ),
        "boundary_clipped": any(
            bool(value.get("boundary_clipped"))
            for value in final_training_evidence
        ),
        "participant_count": len(final_training_evidence),
        "source": "final_rolling_training_fit",
    }


class Stage4DeploymentRefitService:
    """Refit production parameters exclusively from one frozen snapshot."""

    def refit(
        self,
        frozen: Mapping[str, Any],
        samples: Sequence[Mapping[str, Any]],
        *,
        participant_id: Any | None,
        knowledge_cutoff: datetime,
        dataset_snapshot_id: str | None,
    ) -> dict[str, Any]:
        eligible = [
            dict(sample)
            for sample in samples
            if (participant_id is None or str(sample["participant_id"]) == str(participant_id))
            and _aware(sample["observation_created_at"]) <= knowledge_cutoff
        ]
        by_participant: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for sample in eligible:
            by_participant[str(sample["participant_id"])].append(sample)

        parameters: dict[str, dict[str, Any]] = {}
        uncertainty: dict[str, dict[str, Any]] = {}
        evidence: dict[str, dict[str, Any]] = {}
        for participant, participant_samples in by_participant.items():
            fit_samples = []
            for sample in participant_samples:
                observed_at = _aware(sample["observed_at"])
                eligible_slow = [
                    state
                    for state in frozen.get("slow_history", {}).get(participant, [])
                    if state[0] <= knowledge_cutoff and state[3] <= observed_at
                ]
                slow = (
                    max(eligible_slow, key=lambda state: state[0])
                    if eligible_slow
                    else None
                )
                fit_samples.append(
                    {
                        **sample,
                        "recovery": max(
                            float(sample.get("recovery_without_slow") or 0.0),
                            slow[1] if slow else 0.0,
                        ),
                        "recovery_observed": bool(
                            sample.get("recovery_observed_without_slow")
                            or slow is not None
                        ),
                        "sleep_debt": slow[2] if slow else 0.0,
                    }
                )
            priors = [
                value
                for value in frozen.get("brs_history", {}).get(participant, [])
                if value[0] <= knowledge_cutoff
            ]
            prior = max(priors, key=lambda value: value[0])[1] if priors else None
            fitted = fit_workload_candidate_parameters(
                fit_samples,
                trait_resilience=prior,
            )
            rates = estimate_response_rates(fit_samples, fitted)
            local_dates = sorted({str(row["local_date"]) for row in fit_samples})
            transition_count = int(rates["response_transition_count"]) + int(
                rates["recovery_transition_count"]
            )
            parameters[participant] = _candidate_parameters(fitted)
            uncertainty[participant] = _candidate_uncertainty(fitted)
            evidence[participant] = {
                "sample_count": len(fit_samples),
                "day_count": len(local_dates),
                "window_start": local_dates[0] if local_dates else None,
                "window_end": local_dates[-1] if local_dates else None,
                "transition_count": transition_count,
                "identifiability_status": fitted.get("identifiability_status"),
                "boundary_clipped": bool(fitted.get("boundary_clipped")),
                "design_condition_number": fitted.get("design_condition_number"),
                "knowledge_cutoff": knowledge_cutoff.isoformat(),
                "dataset_snapshot_id": dataset_snapshot_id,
                "deployment_refit_version": DEPLOYMENT_REFIT_VERSION,
                "parameter_fit_version": fitted.get("parameter_fit_version"),
                "observation_ids": sorted(
                    str(row.get("observation_id") or "")
                    for row in fit_samples
                    if row.get("observation_id")
                ),
            }
        return {
            "parameters": parameters,
            "uncertainty": uncertainty,
            "evidence": evidence,
        }


class Stage4CandidateReplayService:
    """Compare frozen candidates without maintaining a second model formula."""

    def __init__(self, timezone_name: str):
        self.model = AssessmentModel(timezone_name)
        self.timezone = self.model.timezone

    @staticmethod
    def _calendar_events(calendar: Mapping[str, Any]) -> list[dict[str, Any]]:
        events = []
        for raw in calendar.get("calendar_representation") or []:
            event = dict(raw)
            metadata = dict(event.get("metadata") or {})
            if event.get("workload_prior") is not None:
                semantic = dict(metadata.get("semantic") or {})
                semantic["workload_prior"] = event.get("workload_prior")
                semantic["workload_feature_vector"] = event.get(
                    "workload_feature_vector"
                )
                semantic["workload_model_version"] = event.get(
                    "workload_model_version"
                )
                metadata["semantic"] = semantic
            if event.get("event_type") in {"rest", "nap"}:
                metadata["protected_break"] = True
            event["metadata"] = metadata
            events.append(event)
        return events

    @staticmethod
    def _calendar_recovery(
        calendar: Mapping[str, Any], observed_at: datetime
    ) -> float:
        demanding: list[tuple[datetime, datetime]] = []
        active = 0.0
        for event in calendar.get("calendar_representation") or []:
            try:
                start = _aware(event.get("start_time"))
                end = _aware(event.get("end_time"))
            except (TypeError, ValueError):
                continue
            event_type = str(event.get("event_type") or "").lower()
            metadata = dict(event.get("metadata") or {})
            name = str(event.get("summary") or event.get("name") or "").lower()
            protected = bool(
                metadata.get("protected_break")
                or event_type in {"rest", "nap"}
                or "protected break" in name
                or "保护性休息" in name
            )
            if start <= observed_at < end:
                if event_type == "sleep":
                    active = 1.0
                elif protected:
                    active = max(active, 0.65)
            if event_type not in {"rest", "nap", "sleep", "meal"}:
                demanding.append((start, end))
        if active:
            return active
        previous = [end for _, end in demanding if end <= observed_at]
        following = [start for start, _ in demanding if start > observed_at]
        if not previous or not following:
            return 0.0
        gap = (min(following) - max(previous)).total_seconds() / 60.0
        return 0.35 * min(1.0, max(0.0, gap) / 60.0) if gap >= 10 else 0.0

    def _extract(
        self,
        items: Sequence[Mapping[str, Any]],
        participant_id: Any | None,
    ) -> dict[str, Any]:
        forecasts = {
            str(item["source_id"]): dict(item["metadata"])
            for item in items
            if item["item_type"] == "forecast"
        }
        calendars = {
            str(item["source_id"]): dict(item["metadata"])
            for item in items
            if item["item_type"] == "calendar"
        }
        observations = {
            str(item["source_id"]): dict(item["metadata"])
            for item in items
            if item["item_type"] == "observation"
        }
        observation_history: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in items:
            if item["item_type"] != "observation":
                continue
            metadata = dict(item["metadata"])
            metadata["participant_id"] = str(item["participant_id"])
            observation_history[str(item["participant_id"])].append(metadata)

        slow_history: dict[
            str, list[tuple[datetime, float, float, datetime]]
        ] = defaultdict(list)
        brs_history: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
        for item in items:
            metadata = dict(item["metadata"])
            participant = str(item["participant_id"])
            if item["item_type"] == "slow_state":
                recovery = _number(metadata.get("recent_recovery_quality"))
                sleep_debt = _number(metadata.get("recent_sleep_debt")) or 0.0
                try:
                    available = max(
                        _aware(metadata.get("effective_at")),
                        _aware(metadata.get("created_at")),
                    )
                except (TypeError, ValueError):
                    continue
                slow_history[participant].append(
                    (
                        available,
                        max(0.0, min(1.0, (recovery or 0.0) / 10.0)),
                        sleep_debt,
                        _aware(metadata.get("effective_at")),
                    )
                )
            elif item["item_type"] == "psychometric" and str(
                metadata.get("instrument_name") or ""
            ).upper() == "BRS":
                prior = trait_resilience_prior(metadata.get("scores"))
                if prior is None:
                    continue
                try:
                    available = max(
                        _aware(metadata.get("administered_at")),
                        _aware(metadata.get("created_at")),
                    )
                except (TypeError, ValueError):
                    continue
                brs_history[participant].append((available, prior))

        samples = []
        for item in items:
            if item["item_type"] != "match_source":
                continue
            if participant_id is not None and item["participant_id"] != participant_id:
                continue
            match = dict(item["metadata"])
            forecast_id = str(match.get("forecast_id") or "")
            forecast = forecasts.get(forecast_id) or {}
            calendar = calendars.get(forecast_id) or {}
            context = dict(match.get("context") or {})
            point_time = str(context.get("forecast_point_time") or "")[:5]
            point = next(
                (
                    dict(value)
                    for value in forecast.get("curve") or []
                    if str(value.get("time") or "")[:5] == point_time
                ),
                {},
            )
            observation = observations.get(str(match.get("observation_id"))) or {}
            payload = dict(observation.get("payload") or {})
            actual = _number(match.get("actual_stress"))
            current_prediction = _number(match.get("predicted_stress"))
            if actual is None or current_prediction is None:
                continue
            try:
                observed_at = _aware(match.get("observed_at"))
                observation_created_at = _aware(observation.get("created_at"))
            except (TypeError, ValueError):
                continue
            workload = _number(point.get("workload"))
            workload_observed = workload is not None
            if workload is None:
                raw = _number(context.get("workload_0_10"))
                workload_observed = raw is not None
                workload = (raw or 0.0) / 10.0
            recovery = _number(point.get("recovery_resource")) or 0.0
            recovery_observed = point.get("recovery_resource") is not None
            reported = next(
                (
                    _number(payload.get(key))
                    for key in ("recovery_0_10", "recovery_quality_0_10")
                    if payload.get(key) is not None
                ),
                None,
            )
            if reported is not None:
                recovery = max(recovery, reported / 10.0)
                recovery_observed = True
            eligible_slow = [
                value
                for value in slow_history.get(str(item["participant_id"]), [])
                if value[0] <= observed_at
            ]
            slow = max(eligible_slow, key=lambda value: value[0]) if eligible_slow else None
            calendar_recovery = self._calendar_recovery(calendar, observed_at)
            recovery_without_slow = max(recovery, calendar_recovery)
            recovery_observed_without_slow = bool(
                recovery_observed or calendar_recovery > 0.0
            )
            recovery = max(recovery_without_slow, slow[1] if slow else 0.0)
            recovery_observed = bool(
                recovery_observed or slow is not None or calendar_recovery > 0.0
            )
            current_peak = _trajectory_peak(forecast.get("curve") or [])
            samples.append(
                {
                    **match,
                    **current_peak,
                    "participant_id": str(item["participant_id"]),
                    "local_date": item["local_date"].isoformat(),
                    "observed_at": observed_at.isoformat(),
                    "observation_created_at": observation_created_at.isoformat(),
                    "forecast_id": forecast_id,
                    "match_source_hash": item.get("source_hash"),
                    "actual_stress": actual,
                    "current_prediction": current_prediction,
                    "historical_production_prediction": current_prediction,
                    "workload": max(0.0, min(1.0, workload)),
                    "workload_observed": workload_observed,
                    "recovery": max(0.0, min(1.0, recovery)),
                    "recovery_without_slow": max(
                        0.0, min(1.0, recovery_without_slow)
                    ),
                    "recovery_observed_without_slow": recovery_observed_without_slow,
                    "recovery_observed": recovery_observed,
                    "observed_vitality": next(
                        (
                            _number(payload.get(key))
                            for key in ("energy_0_10", "vitality_0_10")
                            if payload.get(key) is not None
                        ),
                        None,
                    ),
                    "post_event_input": _number(point.get("post_event_input")) or 0.0,
                    "continuous_load": _number(point.get("continuous_load_factor")) or 0.0,
                    "sleep_debt": slow[2] if slow else 0.0,
                    "initial_state": dict(forecast.get("initial_state") or {}),
                    "initial_state_revision": forecast.get(
                        "initial_state_revision"
                    ),
                }
            )
        samples.sort(key=lambda row: (row["local_date"], row["observed_at"], row["participant_id"]))
        return {
            "samples": samples,
            "forecasts": forecasts,
            "calendars": calendars,
            "observation_history": observation_history,
            "brs_history": brs_history,
            "slow_history": slow_history,
        }

    @staticmethod
    def _frozen_initial_state(sample: Mapping[str, Any]) -> dict[str, float]:
        frozen = dict(sample.get("initial_state") or {})
        try:
            return {
                "stress_0_10": max(0.0, min(10.0, float(frozen["stress_0_10"]))),
                "vitality_0_10": max(0.0, min(10.0, float(frozen["vitality_0_10"]))),
            }
        except (KeyError, TypeError, ValueError):
            # Dataset rows created before initial-state provenance was added
            # remain replayable, but the fallback is explicit in evaluation
            # metadata and cannot qualify as complete provenance.
            return {"stress_0_10": 4.0, "vitality_0_10": 7.0}

    @staticmethod
    def _known_observations(
        history: Sequence[Mapping[str, Any]], target: datetime
    ) -> list[dict[str, Any]]:
        known = []
        for observation in history:
            try:
                observed = _aware(observation.get("observed_at"))
                created = _aware(observation.get("created_at"))
            except (TypeError, ValueError):
                continue
            if max(observed, created) >= target:
                continue
            known.append(
                {
                    "type": observation.get("observation_type"),
                    "observed_at": observation.get("observed_at"),
                    "payload": dict(observation.get("payload") or {}),
                }
            )
        return known

    @staticmethod
    def _support(samples: Sequence[Mapping[str, Any]], family: str) -> dict[str, Any]:
        participants = {str(row["participant_id"]) for row in samples}
        days = {str(row["local_date"]) for row in samples}
        ordered: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
        for row in samples:
            ordered[
                (str(row["participant_id"]), str(row["local_date"]))
            ].append(row)
        persistence = recovery_transitions = sustained_episodes = 0
        for rows in ordered.values():
            rows = sorted(rows, key=lambda row: str(row["observed_at"]))
            in_sustained = False
            for row in rows:
                sustained = float(row.get("continuous_load") or 0.0) > 0.0
                if sustained and not in_sustained:
                    sustained_episodes += 1
                in_sustained = sustained
            for previous, current in zip(rows, rows[1:]):
                if float(current.get("post_event_input") or 0.0) > 0.0 and (
                    float(current["actual_stress"]) >= float(previous["actual_stress"]) - 0.5
                ):
                    persistence += 1
                if (
                    (float(previous.get("workload") or 0.0) >= 0.7 or float(previous.get("continuous_load") or 0.0) > 0.0)
                    and float(current.get("recovery") or 0.0) > 0.0
                    and float(current.get("workload") or 0.0) < float(previous.get("workload") or 0.0)
                    and float(current["actual_stress"]) < float(previous["actual_stress"])
                ):
                    recovery_transitions += 1
        counts = {
            "stress_ema_count": len(samples),
            "workload_observation_count": sum(bool(row.get("workload_observed")) for row in samples),
            "workload_level_count": len({round(float(row.get("workload") or 0.0), 6) for row in samples if row.get("workload_observed")}),
            "recovery_observation_count": sum(bool(row.get("recovery_observed")) for row in samples),
            "recovery_level_count": len({round(float(row.get("recovery") or 0.0), 6) for row in samples if row.get("recovery_observed")}),
            "vitality_observation_count": sum(row.get("observed_vitality") is not None for row in samples),
            "post_event_exposure_count": sum(float(row.get("post_event_input") or 0.0) > 0.0 for row in samples),
            "post_event_ema_count": sum(float(row.get("post_event_input") or 0.0) > 0.0 and row.get("actual_stress") is not None for row in samples),
            "stress_persistence_transition_count": persistence,
            "sustained_workload_episode_count": sustained_episodes,
            "continuous_load_level_count": len({round(float(row.get("continuous_load") or 0.0), 6) for row in samples}),
            "post_load_recovery_transition_count": recovery_transitions,
            "participant_count": len(participants),
            "day_count": len(days),
        }
        checks = {
            "stress_ema": counts["stress_ema_count"] > 0,
        }
        if family != "current_m0":
            checks.update(
                {
                    "workload": counts["workload_observation_count"] > 0 and counts["workload_level_count"] >= 2,
                    "recovery": counts["recovery_observation_count"] > 0 and counts["recovery_level_count"] >= 2,
                }
            )
        if family in {"m1", "m2", "m3"}:
            checks["vitality"] = counts["vitality_observation_count"] > 0
        if family == "m2":
            checks.update(
                {
                    key: counts[key] >= threshold
                    for key, threshold in OBSERVABLE_SUPPORT_CONFIG["m2"].items()
                }
            )
        if family == "m3":
            checks.update(
                {
                    key: counts[key] >= threshold
                    for key, threshold in OBSERVABLE_SUPPORT_CONFIG["m3"].items()
                }
            )
        return {
            "version": OBSERVABLE_SUPPORT_CONFIG["version"],
            "supported": all(checks.values()),
            "counts": counts,
            "checks": checks,
            "thresholds": OBSERVABLE_SUPPORT_CONFIG.get(family, {}),
        }

    def compare(
        self,
        items: Sequence[Mapping[str, Any]],
        *,
        participant_id: Any | None,
        requested_family: str,
        config: dict[str, Any],
    ) -> dict[str, Any]:
        frozen = self._extract(items, participant_id)
        samples = frozen["samples"]
        splits = rolling_origin_splits(samples, minimum_training_days=2)
        metric_indices = sorted(
            {
                index
                for split in splits
                for index in split["test_indices"]
            }
        )
        metric_samples = [samples[index] for index in metric_indices]
        evaluation_source_set = {
            "observation_ids": sorted(
                str(row.get("observation_id"))
                for row in metric_samples
                if row.get("observation_id")
            ),
            "forecast_ids": sorted(
                {
                    str(row["forecast_id"])
                    for row in metric_samples
                    if row.get("forecast_id")
                }
            ),
            "match_source_hashes": sorted(
                str(row["match_source_hash"])
                for row in metric_samples
                if row.get("match_source_hash")
            ),
            "promotion_decision_ids": sorted(
                {
                    str((row.get("context") or {}).get("promotion_decision_id"))
                    for row in metric_samples
                    if (row.get("context") or {}).get("promotion_decision_id")
                }
            ),
            "promotion_parameters_hashes": sorted(
                {
                    str(
                        (row.get("context") or {}).get(
                            "promotion_parameters_hash"
                        )
                    )
                    for row in metric_samples
                    if (row.get("context") or {}).get(
                        "promotion_parameters_hash"
                    )
                }
            ),
        }
        config = {**config, "evaluation_source_set": evaluation_source_set}
        if not splits:
            return {
                "config": config,
                "status": "insufficient_rolling_origin_days",
                "rolling_origin": {"version": ROLLING_ORIGIN_VERSION, "split_count": 0, "sample_count": len(samples)},
                "comparison": {},
                "promotion": {},
            }
        prediction_sets = {
            **{family: [] for family in MODEL_FAMILIES},
            "historical_production": [],
        }
        parameter_history = []
        replay_audit = []
        evaluation_parameters: dict[str, dict[str, Any]] = {}
        evaluation_uncertainty: dict[str, dict[str, Any]] = {}
        evaluation_evidence: dict[str, dict[str, Any]] = {}
        initial_state_provenance_complete = True
        for split in splits:
            origin_cutoff = datetime.combine(
                datetime.fromisoformat(split["test_days"][0]).date(),
                time.min,
                self.timezone,
            ).astimezone(timezone.utc)
            split["origin_cutoff"] = origin_cutoff.isoformat()
            training = [
                samples[index]
                for index in split["train_indices"]
                if _aware(samples[index]["observation_created_at"])
                < origin_cutoff
            ]
            testing = [samples[index] for index in split["test_indices"]]

            def with_origin_slow(
                rows: Sequence[Mapping[str, Any]],
            ) -> list[dict[str, Any]]:
                adjusted = []
                for value in rows:
                    participant_slow = [
                        state
                        for state in frozen["slow_history"].get(
                            str(value["participant_id"]), []
                        )
                        if state[0] < origin_cutoff
                        and state[3] <= _aware(value["observed_at"])
                    ]
                    slow = (
                        max(participant_slow, key=lambda state: state[0])
                        if participant_slow
                        else None
                    )
                    adjusted.append(
                        {
                            **value,
                            "recovery": max(
                                float(value.get("recovery_without_slow") or 0.0),
                                slow[1] if slow else 0.0,
                            ),
                            "recovery_observed": bool(
                                value.get("recovery_observed_without_slow")
                                or slow is not None
                            ),
                            "sleep_debt": slow[2] if slow else 0.0,
                        }
                    )
                return adjusted

            training = with_origin_slow(training)
            by_participant: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for sample in testing:
                by_participant[str(sample["participant_id"])].append(sample)
            for participant, participant_testing in by_participant.items():
                participant_training = [row for row in training if row["participant_id"] == participant]
                fit_samples = participant_training
                priors = [
                    value
                    for value in frozen["brs_history"].get(participant, [])
                    if value[0] < origin_cutoff
                ]
                prior = max(priors, key=lambda value: value[0])[1] if priors else None
                m0_fitted = fit_current_m0_parameters_v2(
                    fit_samples,
                    frozen,
                    origin_cutoff,
                    self.model,
                )
                fitted = fit_workload_candidate_parameters(
                    fit_samples,
                    trait_resilience=prior,
                )
                rates = estimate_response_rates(fit_samples, fitted)
                m0_params = {"S_star_init": float(m0_fitted["S_star_init"])}
                candidate_params = _candidate_parameters(fitted)
                candidate_uncertainty = _candidate_uncertainty(fitted)
                transition_count = int(rates["response_transition_count"]) + int(
                    rates["recovery_transition_count"]
                )
                common_history = {
                    "split_index": split["split_index"],
                    "participant_id": participant,
                    "origin_cutoff": origin_cutoff.isoformat(),
                    "training_sample_count": len(fit_samples),
                }
                parameter_history.append(
                    {
                        **common_history,
                        "family": "current_m0",
                        **m0_fitted,
                    }
                )
                candidate_history = {
                    **common_history,
                    **fitted,
                    **rates,
                    **observed_recovery_efficiency(fit_samples),
                }
                parameter_history.extend(
                    {
                        **candidate_history,
                        "family": family,
                    }
                    for family in MODEL_FAMILIES
                    if family != "current_m0"
                )
                participant_slow = [
                    value
                    for value in frozen["slow_history"].get(participant, [])
                    if value[0] < origin_cutoff
                ]
                origin_slow = (
                    max(participant_slow, key=lambda value: value[0])
                    if participant_slow
                    else None
                )
                sleep_debt = origin_slow[2] if origin_slow else 0.0
                full_peak_cache: dict[tuple[str, str], dict[str, Any]] = {}
                daily_origins = {
                    local_day: min(
                        (
                            value
                            for value in participant_testing
                            if value["local_date"] == local_day
                        ),
                        key=lambda value: str(value["observed_at"]),
                    )
                    for local_day in {
                        value["local_date"] for value in participant_testing
                    }
                }
                for sample in participant_testing:
                    target = _aware(sample["observed_at"])
                    calendar = frozen["calendars"].get(sample["forecast_id"]) or {}
                    events = self._calendar_events(calendar)
                    initial_state = self._frozen_initial_state(sample)
                    if not sample.get("initial_state_revision"):
                        initial_state_provenance_complete = False
                    known = self._known_observations(
                        frozen["observation_history"].get(participant, []), target
                    )
                    daily_origin = daily_origins[sample["local_date"]]
                    daily_peak_cutoff = min(
                        _aware(daily_origin["observed_at"]),
                        _aware(daily_origin["observation_created_at"]),
                    )
                    daily_current_peak = {
                        "trajectory_peak_stress": daily_origin.get(
                            "trajectory_peak_stress"
                        ),
                        "trajectory_peak_time": daily_origin.get(
                            "trajectory_peak_time"
                        ),
                    }
                    historical_row = {
                        **sample,
                        **daily_current_peak,
                        "predicted_stress": sample[
                            "historical_production_prediction"
                        ],
                        "prediction_lower": sample.get("prediction_lower"),
                        "prediction_upper": sample.get("prediction_upper"),
                        "split_index": split["split_index"],
                    }
                    prediction_sets["historical_production"].append(
                        historical_row
                    )
                    for family in MODEL_FAMILIES:
                        variant = MODEL_VARIANT_BY_FAMILY[family]
                        replay = (
                            self.model.predict_baseline_m0
                            if family == "current_m0"
                            else self.model.predict_candidate
                        )
                        replay_arguments = {
                            "observations": known,
                            "calendar_events": events,
                            "local_date": sample["local_date"],
                            "initial_state": initial_state,
                            "sleep_debt_hours": sleep_debt,
                        }
                        if family == "current_m0":
                            replay_arguments["baseline_params"] = m0_params
                        else:
                            replay_arguments["model_variant"] = variant
                            replay_arguments["candidate_params"] = candidate_params
                        result = replay(**replay_arguments)
                        point_time = str((sample.get("context") or {}).get("forecast_point_time") or "")[:5]
                        point = next(
                            (row for row in result.trajectory if str(row.get("time") or "")[:5] == point_time),
                            None,
                        )
                        if point is None:
                            continue
                        cache_key = (family, sample["local_date"])
                        if cache_key not in full_peak_cache:
                            origin_calendar = frozen["calendars"].get(
                                daily_origin["forecast_id"]
                            ) or {}
                            origin_known = self._known_observations(
                                frozen["observation_history"].get(participant, []),
                                daily_peak_cutoff,
                            )
                            origin_arguments = {
                                "observations": origin_known,
                                "calendar_events": self._calendar_events(
                                    origin_calendar
                                ),
                                "local_date": sample["local_date"],
                                "initial_state": self._frozen_initial_state(
                                    daily_origin
                                ),
                                "sleep_debt_hours": sleep_debt,
                            }
                            if family == "current_m0":
                                origin_arguments["baseline_params"] = m0_params
                            else:
                                origin_arguments["model_variant"] = variant
                                origin_arguments["candidate_params"] = candidate_params
                            origin = replay(**origin_arguments)
                            full_peak_cache[cache_key] = _trajectory_peak(origin.trajectory)
                        interval = dict(point.get("stress_interval_90_0_10") or {})
                        prediction_sets[family].append(
                            {
                                **sample,
                                **full_peak_cache[cache_key],
                                "predicted_stress": point["stress_0_10"],
                                "prediction_lower": interval.get("lower"),
                                "prediction_upper": interval.get("upper"),
                                "split_index": split["split_index"],
                                "replay_engine": REPLAY_ENGINE_VERSION,
                            }
                        )
                        replay_audit.append(
                            {
                                "split_index": split["split_index"],
                                "participant_id": participant,
                                "family": family,
                                "replayed_model_variant": result.model_variant,
                                "target": target.isoformat(),
                                "origin_cutoff": origin_cutoff.isoformat(),
                                "daily_peak_origin": daily_peak_cutoff.isoformat(),
                                "initial_state_revision": sample.get(
                                    "initial_state_revision"
                                ),
                                "assimilated_observation_count": len(known),
                                "target_observation_assimilated": False,
                                "trajectory_point_count": result.point_count,
                                "interval_source": "LatentUncertainty/prediction_interval",
                            }
                        )
                evaluation_parameters[participant] = candidate_params
                evaluation_uncertainty[participant] = candidate_uncertainty
                evaluation_evidence[participant] = {
                    "participant_id": participant,
                    "split_index": split["split_index"],
                    "sample_count": len(fit_samples),
                    "day_count": len(
                        {str(value["local_date"]) for value in fit_samples}
                    ),
                    "transition_count": transition_count,
                    "identifiability_status": fitted.get(
                        "identifiability_status"
                    ),
                    "boundary_clipped": bool(fitted.get("boundary_clipped")),
                    "design_condition_number": fitted.get(
                        "design_condition_number"
                    ),
                    "training_window_start": (
                        min(str(value["local_date"]) for value in fit_samples)
                        if fit_samples
                        else None
                    ),
                    "training_window_end": (
                        max(str(value["local_date"]) for value in fit_samples)
                        if fit_samples
                        else None
                    ),
                    "parameter_fit_version": fitted.get(
                        "parameter_fit_version"
                    ),
                }

        comparison: dict[str, dict[str, Any]] = {}
        baseline_by_participant = {}
        for participant in sorted({row["participant_id"] for row in samples}):
            metric = comparison_metrics([row for row in prediction_sets["current_m0"] if row["participant_id"] == participant])
            if metric["mae"] is not None:
                baseline_by_participant[participant] = metric["mae"]
        for family in MODEL_FAMILIES:
            metrics = comparison_metrics(prediction_sets[family])
            metrics["model_variant"] = MODEL_VARIANT_BY_FAMILY[family]
            metrics["replay_engine"] = REPLAY_ENGINE_VERSION
            metrics["observable_support"] = self._support(samples, family)
            metrics["participant_effect"] = []
            for participant, baseline_mae in baseline_by_participant.items():
                participant_metrics = comparison_metrics([row for row in prediction_sets[family] if row["participant_id"] == participant])
                if participant_metrics["mae"] is not None:
                    metrics["participant_effect"].append(
                        {
                            "participant_id": participant,
                            "mae_delta_vs_current_m0": round(participant_metrics["mae"] - baseline_mae, 4),
                            "sample_count": participant_metrics["sample_count"],
                        }
                    )
            comparison[family] = metrics
        historical_production = comparison_metrics(
            prediction_sets["historical_production"]
        )
        historical_production.update(
            {
                "model_variant": "frozen_historical_production",
                "replay_engine": "frozen_historical_production",
            }
        )
        aggregate_parameter_evidence = (
            aggregate_evaluation_parameter_gate_evidence(evaluation_evidence)
        )
        promotion = {
            family: promotion_gate(
                comparison["current_m0"],
                comparison[family],
                parameter_evidence=aggregate_parameter_evidence,
            )
            for family in MODEL_FAMILIES
            if family != "current_m0"
        }
        deployment = {
            "parameters": {},
            "uncertainty": {},
            "evidence": {},
        }
        if any(bool(gate.get("passed")) for gate in promotion.values()):
            cutoff_value = config.get("observation_cutoff")
            try:
                knowledge_cutoff = _aware(cutoff_value)
            except (TypeError, ValueError):
                knowledge_cutoff = datetime.max.replace(tzinfo=timezone.utc)
            deployment = Stage4DeploymentRefitService().refit(
                frozen,
                samples,
                participant_id=participant_id,
                knowledge_cutoff=knowledge_cutoff,
                dataset_snapshot_id=(
                    str(config.get("dataset_snapshot_id"))
                    if config.get("dataset_snapshot_id")
                    else None
                ),
            )
        evaluation_config = {
            **config,
            "replay_engine_version": REPLAY_ENGINE_VERSION,
            "observable_support_config_version": OBSERVABLE_SUPPORT_CONFIG["version"],
            "candidate_latent_initialization": {
                "version": CANDIDATE_LATENT_INITIALIZATION_VERSION,
                "perseverative_cognition": "0.0 at frozen day boundary",
                "recovery_debt": (
                    "clip((vitality_baseline-initial_vitality)/80 + "
                    "min(0.25,sleep_debt_hours*0.04), 0, 0.75)"
                ),
            },
            "deployment_refit_version": DEPLOYMENT_REFIT_VERSION,
            "initial_state_provenance_complete": initial_state_provenance_complete,
        }
        return {
            "config": evaluation_config,
            "status": "completed",
            "replay_engine": REPLAY_ENGINE_VERSION,
            "rolling_origin": {"version": ROLLING_ORIGIN_VERSION, "split_count": len(splits), "splits": splits},
            "comparison": comparison,
            "historical_production": historical_production,
            "promotion": promotion,
            "requested_family": requested_family,
            "metrics": comparison.get(requested_family) if requested_family != "all" else None,
            "parameter_history": parameter_history,
            "evaluation_candidate_parameters": evaluation_parameters,
            "evaluation_candidate_uncertainty": evaluation_uncertainty,
            "evaluation_candidate_evidence": evaluation_evidence,
            "evaluation_parameter_gate_evidence": evaluation_evidence,
            "evaluation_parameter_gate_aggregate": aggregate_parameter_evidence,
            "deployment_parameters": deployment["parameters"],
            "deployment_uncertainty": deployment["uncertainty"],
            "deployment_evidence": deployment["evidence"],
            "replay_audit": replay_audit,
        }
