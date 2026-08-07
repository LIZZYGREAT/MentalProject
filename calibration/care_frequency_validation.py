"""Reproducible synthetic burden check for the non-clinical care policy."""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
import random
from typing import Any, Dict, List, Mapping

from calibration.simulation_runner import run_simulation_for_calibration


def run_synthetic_care_frequency_check(
    base_params: Mapping[str, Any],
    *,
    days: int = 160,
    seed: int = 20260731,
) -> Dict[str, Any]:
    """Check for obvious over-contact or silence; not a population estimate."""

    rng = random.Random(seed)
    records = []
    for index in range(max(1, int(days))):
        draw = rng.random()
        load = "light" if draw < 0.36 else "moderate" if draw < 0.88 else "heavy"
        events = _synthetic_schedule(rng, index, load)
        simulation = run_simulation_for_calibration(
            f"2026-01-{index % 28 + 1:02d}",
            events,
            user_params=deepcopy(dict(base_params)),
            weave_routines=False,
        )
        records.append(
            {
                "load": load,
                "alert_count": len(simulation["alerts"]),
                "peak_stress": max(
                    (float(row.get("S", 0.0)) for row in simulation["results"]),
                    default=0.0,
                ),
            }
        )

    distribution = Counter(record["alert_count"] for record in records)
    by_load = {}
    for load in ("light", "moderate", "heavy"):
        subset = [record for record in records if record["load"] == load]
        by_load[load] = {
            "days": len(subset),
            "mean_alerts": _mean([record["alert_count"] for record in subset]),
            "care_day_rate": _mean(
                [float(record["alert_count"] > 0) for record in subset]
            ),
            "mean_peak_stress": _mean(
                [record["peak_stress"] for record in subset]
            ),
        }
    overall_mean = _mean([record["alert_count"] for record in records])
    care_day_rate = _mean([float(record["alert_count"] > 0) for record in records])
    guardrails = {
        "light_care_day_rate_at_most_10_percent": (
            by_load["light"]["care_day_rate"] <= 0.10
        ),
        "heavy_care_day_rate_at_least_50_percent": (
            by_load["heavy"]["care_day_rate"] >= 0.50
        ),
        "overall_mean_between_0_15_and_0_80": 0.15 <= overall_mean <= 0.80,
        "no_day_exceeds_configured_regular_budget": max(distribution, default=0) <= 3,
    }
    return {
        "schema_version": "synthetic_care_frequency_check.v1",
        "evidence_type": "engineering_sanity_check_not_population_validation",
        "seed": seed,
        "days": len(records),
        "distribution": {str(key): distribution[key] for key in sorted(distribution)},
        "mean_alerts_per_day": overall_mean,
        "care_day_rate": care_day_rate,
        "by_load": by_load,
        "guardrails": guardrails,
        "passed": all(guardrails.values()),
    }


def _synthetic_schedule(
    rng: random.Random,
    day_index: int,
    load: str,
) -> List[Dict[str, Any]]:
    if load == "light":
        count = rng.choice([0, 0, 1])
        duration_range = (45, 90)
        threat_range = (0.12, 0.30)
        effort_range = (0.30, 0.55)
    elif load == "moderate":
        count = rng.choice([1, 2, 2, 3])
        duration_range = (75, 150)
        threat_range = (0.42, 0.68)
        effort_range = (0.55, 0.80)
    else:
        count = rng.choice([2, 3, 3])
        duration_range = (120, 210)
        threat_range = (0.78, 0.98)
        effort_range = (0.82, 1.00)

    events = []
    cursor = 8 * 60 + rng.randint(0, 45)
    for event_index in range(count):
        duration = rng.randint(*duration_range)
        if cursor + duration > 21 * 60:
            break
        start = _time(cursor)
        end = _time(cursor + duration)
        threat = rng.uniform(*threat_range)
        effort = rng.uniform(*effort_range)
        events.append(
            {
                "id": f"synthetic-{day_index}-{event_index}",
                "event_type": "task" if event_index % 2 == 0 else "course",
                "task_type": "exam" if load == "heavy" else "general",
                "summary": f"{load}-load-{event_index}",
                "start_time": start,
                "end_time": end,
                "objective": {
                    "deadline": min(1.0, threat + rng.uniform(-0.1, 0.1)),
                    "social_evaluation": min(1.0, threat * 0.75),
                    "unfinished": 0.8 if load == "heavy" else 0.2,
                },
                "appraisal": {
                    "threat": threat,
                    "challenge": max(0.05, 0.75 - threat * 0.5),
                    "control": max(0.05, 1.0 - threat * 0.75),
                    "importance": min(1.0, threat + 0.1),
                    "uncertainty": min(1.0, threat * 0.85),
                    "expected_effort": effort,
                },
            }
        )
        cursor += duration + rng.randint(20, 75)
    return events


def _time(minutes: int) -> str:
    minutes %= 1440
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _mean(values: List[float]) -> float:
    return sum(values) / len(values) if values else 0.0
