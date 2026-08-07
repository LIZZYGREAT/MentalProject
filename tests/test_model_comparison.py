from __future__ import annotations

import unittest

from calibration.model_comparison import run_nested_model_comparison
from calibration.validation_protocol import (
    posterior_predictive_checks,
    run_engineering_validation_protocol,
)
from entry.config import GLOBAL_DEFAULT_CONFIG


class NestedModelComparisonTests(unittest.TestCase):
    def _samples(self):
        samples = []
        for day in range(1, 9):
            samples.append(
                {
                    "date": f"2026-07-{day:02d}",
                    "events": [
                        {
                            "id": f"task-{day}",
                            "event_type": "task",
                            "task_type": "general",
                            "summary": "学习任务",
                            "start_time": "09:00",
                            "end_time": "11:00",
                            "appraisal": {
                                "threat": 0.4,
                                "challenge": 0.6,
                                "control": 0.6,
                                "importance": 0.6,
                                "uncertainty": 0.3,
                                "expected_effort": 0.7,
                            },
                        }
                    ],
                    "observations": [
                        {
                            "time": "08:00",
                            "stress": 4.5,
                            "vitality": 7.0,
                            "perseverative_cognition": 0.2,
                        },
                        {
                            "time": "10:30",
                            "stress": 6.0,
                            "vitality": 5.8,
                            "perseverative_cognition": 0.3,
                        },
                        {
                            "time": "18:00",
                            "stress": 4.8,
                            "vitality": 5.5,
                            "perseverative_cognition": 0.1,
                        },
                    ],
                    "weave_routines": False,
                }
            )
        return samples

    def test_comparison_holds_out_complete_dates_and_keeps_discrete_baselines(self):
        report = run_nested_model_comparison(
            self._samples(),
            GLOBAL_DEFAULT_CONFIG,
            holdout_fraction=0.25,
        )
        self.assertEqual(len(report["split"]["test_dates"]), 2)
        self.assertFalse(report["split"]["adjacent_points_randomly_split"])
        self.assertEqual(set(report["candidate_reports"]), {"m0", "m1", "m2", "m3"})
        self.assertEqual(
            set(report["m1_coupling_reports"]),
            {"m1-a", "m1-b", "m1-c", "m1-d"},
        )
        self.assertEqual(
            report["candidate_reports"]["m2"]["active_states"],
            ["S", "V", "P"],
        )
        self.assertEqual(
            set(report["discrete_baselines"]),
            {"individual_mean", "previous_value", "ar1", "var"},
        )
        self.assertEqual(
            set(report["model_sequence"]),
            {
                "m0_constant",
                "m0_event_regression",
                "m0_continuous_time",
                "m1",
                "m2",
                "m3",
                "m4_hierarchical",
            },
        )
        self.assertEqual(
            report["model_sequence"]["m0_event_regression"]["status"],
            "evaluated_on_complete_later_dates",
        )
        self.assertIsNotNone(
            report["candidate_reports"]["m0"]["test"]["stress_rmse"]
        )
        self.assertFalse(
            report["model_sequence"]["m4_hierarchical"]["eligible_for_hierarchical_fit"]
        )
        self.assertEqual(
            report["event_time_shape"]["flexible_piecewise_discovery_on_train_dates"][
                "data_scope"
            ],
            "training_dates_only",
        )
        self.assertFalse(
            report["event_time_shape"]["held_out_kernel_comparison"][
                "automatic_kernel_change"
            ]
        )

    def test_missing_posterior_evidence_never_auto_promotes_complex_state(self):
        report = run_nested_model_comparison(
            self._samples(),
            GLOBAL_DEFAULT_CONFIG,
            holdout_fraction=0.25,
        )
        recommendation = report["recommendation"]
        self.assertEqual(recommendation["active_variant"], "m0")
        self.assertFalse(recommendation["automatic_promotion_allowed"])
        self.assertIsNone(
            recommendation["retention_gates"]["m1"]["posterior_not_prior_dominated"]
        )

    def test_engineering_protocol_covers_required_counterfactuals(self):
        sample = self._samples()[0]
        report = run_engineering_validation_protocol(
            sample,
            GLOBAL_DEFAULT_CONFIG,
            model_variant="m3",
        )
        self.assertEqual(
            report["evidence_type"],
            "engineering_sanity_check_not_empirical_validation",
        )
        self.assertTrue(report["event_cancellation"]["cancelled_peak_reduced"])
        self.assertTrue(report["completion_status"]["unfinished_recovery_is_slower"])
        self.assertTrue(report["step_consistency"]["within_tolerance"])

    def test_posterior_predictive_summary_does_not_invent_draws(self):
        report = posterior_predictive_checks(
            [40.0, 50.0, 45.0],
            [[39.0, 49.0, 44.0], [42.0, 51.0, 46.0]],
        )
        self.assertEqual(report["status"], "evaluated")
        self.assertEqual(report["draw_count"], 2)


if __name__ == "__main__":
    unittest.main()
