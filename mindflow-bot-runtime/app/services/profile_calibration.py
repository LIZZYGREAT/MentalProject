"""Conservative longitudinal calibration for the identifiable M0 parameters."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from statistics import mean, median
from typing import Any
from zoneinfo import ZoneInfo

from app.repositories import (
    ForecastSnapshotRepository,
    LearnedProfileRepository,
    ObservationRepository,
    PsychometricAssessmentRepository,
)
from app.services.model_comparison import (
    estimate_reactivity_and_recovery,
    estimate_response_rates,
    observed_recovery_efficiency,
    trait_resilience_prior,
)


def _minute(value: Any) -> int | None:
    text = str(value or "")
    try:
        if "T" in text:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return parsed.hour * 60 + parsed.minute
        hour, minute = (int(part) for part in text[:5].split(":"))
        return hour * 60 + minute
    except (TypeError, ValueError):
        return None


def _curve_stress(point: dict[str, Any]) -> float | None:
    raw = point.get("stress_0_10", point.get("S"))
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value / 10.0 if value > 10.0 else value


def _calendar_recovery_resource(
    forecast: dict[str, Any], observed_at: datetime
) -> float:
    """Derive calendar gap/protected break/sleep from causal Forecast input."""

    current = observed_at.astimezone(timezone.utc)
    demanding: list[tuple[datetime, datetime]] = []
    active = 0.0
    for event in (
        (forecast.get("output") or {}).get("classified_calendar_events") or []
    ):
        try:
            start = datetime.fromisoformat(
                str(event.get("start_time") or "").replace("Z", "+00:00")
            )
            end = datetime.fromisoformat(
                str(event.get("end_time") or "").replace("Z", "+00:00")
            )
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            if end.tzinfo is None:
                end = end.replace(tzinfo=timezone.utc)
            start, end = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
        except (TypeError, ValueError):
            continue
        event_type = str(event.get("event_type") or "").lower()
        metadata = dict(event.get("metadata") or {})
        name = str(event.get("summary") or event.get("name") or "").lower()
        if start <= current < end:
            if event_type == "sleep":
                active = 1.0
            elif (
                metadata.get("protected_break")
                or event_type in {"rest", "nap"}
                or "protected break" in name
                or "保护性休息" in name
            ):
                active = max(active, 0.65)
        if event_type not in {"rest", "nap", "sleep", "meal"}:
            demanding.append((start, end))
    if active:
        return active
    previous = [end for _, end in demanding if end <= current]
    following = [start for start, _ in demanding if start > current]
    if not previous or not following:
        return 0.0
    gap_minutes = (min(following) - max(previous)).total_seconds() / 60.0
    return 0.35 * min(1.0, gap_minutes / 60.0) if gap_minutes >= 10 else 0.0


class ProfileCalibrationService:
    MIN_DAYS = 7
    MIN_MATCHED_SAMPLES = 14
    WINDOW_DAYS = 14

    def __init__(
        self, observations: ObservationRepository,
        forecasts: ForecastSnapshotRepository,
        learned_profiles: LearnedProfileRepository,
        timezone_name: str,
        psychometrics: PsychometricAssessmentRepository | None = None,
    ):
        self.observations = observations
        self.forecasts = forecasts
        self.learned_profiles = learned_profiles
        self.timezone = ZoneInfo(timezone_name)
        self.psychometrics = psychometrics

    def causal_samples(
        self, participant_id: Any, *, through: date
    ) -> list[dict[str, Any]]:
        """Build auditable samples from forecasts that predate each target."""

        matched: list[dict[str, Any]] = []
        for observation in self.observations.recent(participant_id, limit=100):
            payload = observation.get("payload") or {}
            try:
                actual = float(payload["stress_0_10"])
                observed = datetime.fromisoformat(observation["observed_at"])
                created = datetime.fromisoformat(observation["created_at"])
            except (KeyError, TypeError, ValueError):
                continue
            observed_utc = (
                observed.replace(tzinfo=timezone.utc)
                if observed.tzinfo is None else observed.astimezone(timezone.utc)
            )
            created_utc = (
                created.replace(tzinfo=timezone.utc)
                if created.tzinfo is None else created.astimezone(timezone.utc)
            )
            observed_local = observed_utc.astimezone(self.timezone)
            local_day = observed_local.date()
            if local_day > through or local_day < through - timedelta(days=self.WINDOW_DAYS - 1):
                continue
            # Both constraints matter for backfilled observations: the source
            # forecast must precede the represented state and its DB ingest.
            causal_cutoff = min(observed_utc, created_utc)
            forecast = self.forecasts.current_at(
                participant_id, local_day, causal_cutoff
            )
            if forecast is None:
                continue
            minute = observed_local.hour * 60 + observed_local.minute
            candidates = []
            for point in forecast.get("curve") or []:
                point_minute = _minute(point.get("time"))
                stress = _curve_stress(point)
                if point_minute is not None and stress is not None:
                    candidates.append((abs(point_minute - minute), stress, point))
            if not candidates:
                continue
            _, predicted, point = min(candidates, key=lambda item: item[0])
            reported_recovery = payload.get(
                "recovery_0_10", payload.get("recovery_quality_0_10")
            )
            try:
                reported_recovery_value = max(
                    0.0, min(1.0, float(reported_recovery) / 10.0)
                )
            except (TypeError, ValueError):
                reported_recovery_value = 0.0
            matched.append({
                "participant_id": str(participant_id),
                "actual": actual,
                "actual_stress": actual,
                "predicted": predicted,
                "workload": float(point.get("workload") or 0.0),
                "recovery": max(
                    float(point.get("recovery_resource") or 0.0),
                    reported_recovery_value,
                    _calendar_recovery_resource(forecast, observed_utc),
                ),
                "observed_at": observed_utc.isoformat(),
                "stress_event_since_last": bool(payload.get("stress_event_since_last")),
                "local_date": local_day,
                "observation_id": observation.get("id"),
                "forecast_id": forecast.get("id"),
                "forecast_version": forecast.get("forecast_version"),
                "forecast_generated_at": forecast.get("generated_at"),
                "causal_cutoff": causal_cutoff.isoformat(),
            })
        return matched

    def maybe_calibrate(self, participant_id: Any, *, through: date) -> dict[str, Any]:
        matched = self.causal_samples(participant_id, through=through)

        days = sorted({item["local_date"] for item in matched})
        if len(days) < self.MIN_DAYS or len(matched) < self.MIN_MATCHED_SAMPLES:
            return {
                "status": "insufficient_evidence",
                "sample_count": len(matched),
                "day_count": len(days),
                "minimum_sample_count": self.MIN_MATCHED_SAMPLES,
                "minimum_day_count": self.MIN_DAYS,
            }
        current = self.learned_profiles.latest(participant_id)
        if (
            current is not None
            and current["window_end"] == days[-1].isoformat()
            and int(current["sample_count"]) >= len(matched)
        ):
            return {"status": "unchanged", "learned_profile": current}

        residuals = [item["actual"] - item["predicted"] for item in matched]
        previous = dict(current["parameters"] if current else {})
        baseline = float(previous.get("S_star_init", 50.0))
        # A single calibration run can move the 0–100 baseline by at most two
        # points, preventing one unusual week from rewriting the participant.
        baseline_step = max(-2.0, min(2.0, median(residuals) * 2.0))
        parameters: dict[str, Any] = {
            **previous,
            "S_star_init": round(max(25.0, min(75.0, baseline + baseline_step)), 3),
        }

        cutoff = (
            datetime.combine(
                through + timedelta(days=1),
                datetime.min.time(),
                self.timezone,
            )
            - timedelta(microseconds=1)
        ).astimezone(timezone.utc)
        brs = (
            self.psychometrics.latest_by_instrument_as_of(
                participant_id,
                "BRS",
                state_cutoff=cutoff,
                knowledge_cutoff=cutoff,
            )
            if self.psychometrics is not None
            else None
        )
        resilience = trait_resilience_prior(
            (brs or {}).get("scores") if brs else None
        )
        parameter_estimate = estimate_reactivity_and_recovery(
            matched,
            trait_resilience=resilience,
        )
        response_rates = estimate_response_rates(matched, parameter_estimate)
        recovery_efficiency = observed_recovery_efficiency(matched)
        existing_ctssm = dict(previous.get("ctssm_params") or {})
        identified_rates = {
            key: value
            for key, value in response_rates.items()
            if key.endswith("_per_hour") and value is not None
        }
        parameters["ctssm_params"] = {
            **existing_ctssm,
            "workload_stress_gain": round(
                parameter_estimate["workload_reactivity_beta"] * 10.0, 3
            ),
            "recovery_stress_gain": round(
                parameter_estimate["recovery_beta"] * 10.0, 3
            ),
            "trait_resilience_prior": resilience,
            **identified_rates,
            "response_transition_count": response_rates[
                "response_transition_count"
            ],
            "recovery_transition_count": response_rates[
                "recovery_transition_count"
            ],
            **recovery_efficiency,
        }

        with_event = [
            item["actual"] - item["predicted"]
            for item in matched if item["stress_event_since_last"]
        ]
        without_event = [
            item["actual"] - item["predicted"]
            for item in matched if not item["stress_event_since_last"]
        ]
        if len(with_event) >= 5 and len(without_event) >= 5:
            old_gain = float((previous.get("ctssm_params") or {}).get("event_stress_gain", 30.0))
            contrast = mean(with_event) - mean(without_event)
            gain_step = max(-1.0, min(1.0, contrast * 0.5))
            parameters["ctssm_params"] = {
                **dict(parameters.get("ctssm_params") or {}),
                "event_stress_gain": round(max(20.0, min(40.0, old_gain + gain_step)), 3),
            }

        saved = self.learned_profiles.save(
            participant_id, parameters=parameters, sample_count=len(matched),
            day_count=len(days), confidence=min(1.0, len(matched) / 42.0),
            window_start=days[0], window_end=days[-1],
            source="calibration.v2-resilience-recovery",
            model_version="mindflow-ctssm-runtime-v8",
            uncertainty={
                "S_star_init": {"std_error": parameter_estimate["uncertainty"]["stress_baseline_0_10"]["std_error"]},
                "ctssm_params": {
                    "workload_stress_gain": parameter_estimate["uncertainty"]["workload_reactivity_beta"],
                    "recovery_stress_gain": parameter_estimate["uncertainty"]["recovery_beta"],
                },
            },
        )
        return {"status": "calibrated", "learned_profile": saved}


def layered_profile(
    explicit_row: dict[str, Any] | None,
    learned_row: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    explicit = dict(explicit_row["profile"] if explicit_row else {})
    learned = dict(learned_row["parameters"] if learned_row else {})
    explicit_params = dict(explicit.get("model_params") or explicit.get("params") or {})
    effective_params = dict(learned)
    for key, value in explicit_params.items():
        if isinstance(value, dict) and isinstance(effective_params.get(key), dict):
            effective_params[key] = {**effective_params[key], **value}
        else:
            effective_params[key] = value
    effective = {**explicit, "model_params": effective_params}
    layers = {
        "precedence": ["system_defaults", "learned", "explicit"],
        "explicit_version": explicit_row.get("version") if explicit_row else None,
        "learned_version": learned_row.get("version") if learned_row else None,
        "learned_confidence": learned_row.get("confidence") if learned_row else None,
    }
    return effective, layers
