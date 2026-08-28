from datetime import date
import math

from app.services.forecast_initial_state import ForecastInitialStateResolver
from app.services.retrospective_reconstructor import RetrospectiveReconstructor


def _curve():
    return [
        {
            "time": f"{index // 12:02d}:{index % 12 * 5:02d}",
            "stress_0_10": 3.0,
            "vitality_0_10": 5.13,
        }
        for index in range(288)
    ]


def _review():
    return {
        "start_stress": 3,
        "peak_stress": 5,
        "end_stress": 6,
        "start_energy": 6,
        "end_energy": 4,
        "energy_consumption": 5,
        "peak_period": "evening",
        "raw": {},
    }


def test_forward_terminal_uses_explicit_2400_output_not_2355_curve_point():
    reconstructor = RetrospectiveReconstructor()
    curve, analysis, diagnostics = reconstructor.reconstruct(
        _curve(),
        _review(),
        source_terminal_state={"stress_0_10": 4.0, "vitality_0_10": 5.0},
        end_anchor_minute=22 * 60,
        end_anchor_source="scheduled_review_time",
        review_local_date="2030-01-15",
        submitted_local_date="2030-01-15",
    )

    expected_stress = round(4.0 + 0.35 * math.exp(-120 / 720) * 3.0, 3)
    assert curve[-1]["time"] == "23:55"
    assert analysis["curve_last_point_state"]["stress_0_10"] == curve[-1]["stress_0_10"]
    assert analysis["forward_terminal_state"]["stress_0_10"] == expected_stress
    assert analysis["terminal_state"]["stress_0_10"] == expected_stress
    assert diagnostics["algorithm_version"] == "anchor-residual-kernel-v3"


def test_current_point_at_t_forecast_missing_output_falls_back_to_profile_not_2355():
    resolver = ForecastInitialStateResolver()
    target = date(2030, 1, 16)
    resolved = resolver.resolve(
        target,
        date(2030, 1, 15),
        previous_day_forecast={
            "id": "forecast",
            "forecast_version": "v1",
            "algorithm_version": "mindflow-ctssm-runtime-v7",
            "curve": _curve(),
            "output": {},
        },
        baseline_state={"stress_0_10": 2, "vitality_0_10": 8},
    )

    assert resolved.mode == "profile_default"
    assert resolved.model_override == {"stress_0_10": 2.0, "vitality_0_10": 8.0}


def test_legacy_forecast_can_still_use_legacy_curve_terminal_convention():
    resolved = ForecastInitialStateResolver().resolve(
        date(2030, 1, 16),
        date(2030, 1, 15),
        previous_day_forecast={
            "id": "legacy",
            "forecast_version": "legacy-v1",
            "algorithm_version": "mindflow-ctssm-runtime-v6",
            "curve": _curve(),
            "output": {},
        },
    )

    assert resolved.mode == "previous_day_forecast"
    assert resolved.model_override == {"stress_0_10": 3.0, "vitality_0_10": 5.13}
