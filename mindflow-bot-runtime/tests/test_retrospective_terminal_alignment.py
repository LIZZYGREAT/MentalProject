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
        "peak_stress": 7,
        "end_stress": 6,
        "start_energy": 6,
        "end_energy": 4,
        "energy_consumption": 5,
        "peak_period": "evening",
        "raw": {},
    }


def _reconstruct(review):
    return RetrospectiveReconstructor().reconstruct(
        _curve(),
        review,
        source_terminal_state={"stress_0_10": 4.0, "vitality_0_10": 5.0},
        end_anchor_minute=22 * 60,
        end_anchor_source="scheduled_review_time",
        review_local_date="2030-01-15",
        submitted_local_date="2030-01-15",
    )


def _series(curve, field):
    return [point[field] for point in curve]


def test_daily_review_numerical_anchor_model_semantics():
    baseline_curve, baseline_analysis, baseline_diagnostics = _reconstruct(_review())

    cases = (
        ("start_stress", 4, "stress_0_10", False),
        ("start_energy", 2, "vitality_0_10", False),
        ("peak_stress", 9, "stress_0_10", False),
        ("end_stress", 2, "stress_0_10", True),
        ("end_energy", 9, "vitality_0_10", True),
    )
    for field, value, curve_field, changes_terminal in cases:
        review = _review()
        review[field] = value
        curve, analysis, _diagnostics = _reconstruct(review)
        assert _series(curve, curve_field) != _series(baseline_curve, curve_field)
        terminal_field = curve_field
        if changes_terminal:
            assert (
                analysis["forward_terminal_state"][terminal_field]
                != baseline_analysis["forward_terminal_state"][terminal_field]
            )
        else:
            assert (
                analysis["forward_terminal_state"]
                == baseline_analysis["forward_terminal_state"]
            )

    period_review = _review()
    period_review["peak_period"] = "morning"
    period_curve, _period_analysis, period_diagnostics = _reconstruct(period_review)
    assert (
        period_diagnostics["peak_anchor_time"]
        != baseline_diagnostics["peak_anchor_time"]
    )
    assert _series(period_curve, "stress_0_10") != _series(
        baseline_curve, "stress_0_10"
    )


def test_daily_review_diagnostics_and_text_do_not_change_model_output():
    baseline_curve, baseline_analysis, _diagnostics = _reconstruct(_review())

    for field, value in (
        ("energy_consumption", 10),
        ("energy_consumption", None),
        ("main_stressor", "连续会议"),
        ("recovery_note", "散步"),
        ("free_text", "今天睡眠不足"),
    ):
        review = _review()
        review[field] = value
        curve, analysis, _changed_diagnostics = _reconstruct(review)
        assert curve == baseline_curve
        assert analysis["forward_terminal_state"] == baseline_analysis[
            "forward_terminal_state"
        ]


def test_inconsistent_peak_is_retained_as_diagnostic_but_not_curve_anchor():
    first_review = _review()
    first_review["peak_stress"] = 2
    first_curve, first_analysis, first_diagnostics = _reconstruct(first_review)

    second_review = _review()
    second_review["peak_stress"] = 1
    second_curve, second_analysis, second_diagnostics = _reconstruct(second_review)

    assert first_diagnostics["peak_consistency"] is False
    assert first_diagnostics["peak_anchor_used"] is False
    assert first_diagnostics["reported_peak_stress"] == 2
    assert first_diagnostics["peak_anchor_reason"] == (
        "inconsistent_reported_peak_not_used"
    )
    assert "peak_stress" not in {
        anchor["name"] for anchor in first_diagnostics["anchors"]
    }
    assert second_diagnostics["reported_peak_stress"] == 1
    assert first_curve == second_curve
    assert first_analysis == second_analysis


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
    assert diagnostics["algorithm_version"] == "anchor-residual-kernel-v4"


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
