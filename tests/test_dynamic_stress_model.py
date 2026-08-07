from __future__ import annotations

import unittest
from copy import deepcopy

from algorithm.dynamic_state_model import assess_event
from calibration.metrics import closest_result_at
from calibration.care_frequency_validation import run_synthetic_care_frequency_check
from calibration.simulation_runner import run_simulation_for_calibration
from calibration.semantic_validation import run_numerical_semantic_check
from entity.user import User
from entry.config import GLOBAL_DEFAULT_CONFIG
from utils.alert_monitor import AlertMonitor
from utils.event_factory import EventFactory


def candidate_params(variant: str = "m3", **overrides):
    params = deepcopy(GLOBAL_DEFAULT_CONFIG)
    params["model_family"] = {
        "m0": "stress-ctssm.m0",
        "m1": "stress-vitality-ctssm.m1",
        "m2": "stress-vitality-pc-ctssm.m2",
        "m3": "stress-vitality-pc-fatigue-ctssm.m3",
    }[variant]
    params["model_selection"] = {
        **params["model_selection"],
        "active_variant": variant,
        "status": "research_candidate_run",
    }
    params.update(overrides)
    return params


class EventAppraisalTests(unittest.TestCase):
    def test_appraisal_changes_stress_more_than_task_demand(self):
        base = {
            "event_type": "task",
            "task_type": "general",
            "start_time": "14:00",
            "end_time": "16:00",
            "objective": {
                "cognitive_demand": 0.9,
                "physical_demand": 0.1,
                "deadline": 0.4,
                "social_evaluation": 0.3,
                "uncontrollability": 0.3,
            },
        }
        liked, threatened = EventFactory.create_from_json(
            [
                {
                    **base,
                    "id": "liked",
                    "summary": "喜欢的高难度编程",
                    "appraisal": {
                        "threat": 0.1,
                        "challenge": 0.9,
                        "control": 0.9,
                        "importance": 0.7,
                        "uncertainty": 0.1,
                        "expected_effort": 0.9,
                    },
                },
                {
                    **base,
                    "id": "threatened",
                    "summary": "高威胁任务",
                    "appraisal": {
                        "threat": 0.9,
                        "challenge": 0.1,
                        "control": 0.1,
                        "importance": 0.9,
                        "uncertainty": 0.9,
                        "expected_effort": 0.9,
                    },
                },
            ]
        )
        liked_assessment = assess_event(liked)
        threatened_assessment = assess_event(threatened)

        self.assertGreater(
            threatened_assessment.stress_intensity,
            liked_assessment.stress_intensity + 0.20,
        )
        self.assertLess(
            abs(
                threatened_assessment.task_demand
                - liked_assessment.task_demand
            ),
            0.05,
        )
        self.assertGreater(
            threatened_assessment.pre_weight,
            liked_assessment.pre_weight,
        )

    def test_event_factory_preserves_explicit_theory_fields(self):
        event = EventFactory.create_from_json(
            [
                {
                    "id": "explicit",
                    "event_type": "task",
                    "task_type": "ddl",
                    "summary": "提交",
                    "start_time": "10:00",
                    "end_time": "11:00",
                    "objective": {"unfinished": 1.0},
                    "appraisal": {"threat": 0.8},
                    "recovery": {"detach": 0.2},
                }
            ]
        )[0]
        self.assertEqual(event.metadata["objective"]["unfinished"], 1.0)
        self.assertEqual(event.metadata["appraisal"]["threat"], 0.8)
        self.assertEqual(event.task_type, "ddl")


class ContinuousTimeDynamicsTests(unittest.TestCase):
    EVENT_SET = [
        {
            "id": "deadline",
            "event_type": "task",
            "task_type": "ddl",
            "summary": "项目截止",
            "start_time": "09:00",
            "end_time": "12:00",
            "objective": {"deadline": 1.0, "unfinished": 0.8},
            "appraisal": {
                "threat": 0.8,
                "importance": 0.9,
                "control": 0.3,
                "uncertainty": 0.7,
                "expected_effort": 0.9,
                "rumination": 0.7,
            },
        },
        {
            "id": "recovery",
            "event_type": "rest",
            "summary": "安静休息",
            "start_time": "12:30",
            "end_time": "13:00",
            "recovery": {"detach": 0.9, "relax": 0.9, "control": 0.9},
        },
        {
            "id": "course",
            "event_type": "course",
            "summary": "课程",
            "start_time": "14:00",
            "end_time": "16:00",
            "appraisal": {
                "threat": 0.35,
                "challenge": 0.7,
                "importance": 0.6,
                "control": 0.7,
                "uncertainty": 0.2,
                "expected_effort": 0.7,
            },
        },
    ]

    def test_state_ranges_and_expected_directions(self):
        output = run_simulation_for_calibration(
            "2026-07-31",
            self.EVENT_SET,
            user_params=candidate_params("m3"),
            weave_routines=False,
        )
        rows = output["results"]
        before = closest_result_at(rows, "08:00")
        during = closest_result_at(rows, "12:00")
        after_recovery = closest_result_at(rows, "13:00")

        self.assertGreater(during["S"], before["S"] + 15.0)
        self.assertLess(during["V"], before["V"] - 10.0)
        self.assertGreater(during["F"], 0.30)
        self.assertLess(after_recovery["S"], during["S"])
        self.assertLess(after_recovery["F"], during["F"])
        self.assertGreater(after_recovery["V"], during["V"])
        for row in rows:
            self.assertGreaterEqual(row["S"], 0.0)
            self.assertLessEqual(row["S"], 100.0)
            self.assertGreaterEqual(row["V"], 0.0)
            self.assertLessEqual(row["V"], 100.0)
            self.assertGreaterEqual(row["P"], 0.0)
            self.assertLessEqual(row["P"], 1.0)
            self.assertGreaterEqual(row["F"], 0.0)
            self.assertLessEqual(row["F"], 1.0)

    def test_anticipation_and_unfinished_aftermath_are_visible(self):
        output = run_simulation_for_calibration(
            "2026-07-31",
            self.EVENT_SET,
            user_params=candidate_params("m3"),
            weave_routines=False,
        )
        rows = output["results"]
        pre = closest_result_at(rows, "08:30")
        post = closest_result_at(rows, "12:20")
        self.assertGreater(pre["anticipatory_input"], 0.0)
        self.assertGreater(pre["P"], 0.0)
        self.assertGreater(post["post_event_input"], 0.0)
        self.assertGreater(post["P"], 0.0)

    def test_explicit_cross_day_unfinished_context_remains_bounded_but_visible(self):
        baseline_user = User(
            params=candidate_params("m0"),
            load_from_file=False,
        )
        carry_user = User(
            params=candidate_params("m0"),
            load_from_file=False,
        )
        baseline = baseline_user.solver.simulate_day(
            [],
            50.0,
            72.0,
            "2026-08-01",
        )[0]
        carried = carry_user.solver.simulate_day(
            [],
            50.0,
            72.0,
            "2026-08-01",
            cross_day_context={
                "unfinished_load": 0.75,
                "unfinished_tasks": [
                    {"event_name": "昨日项目DDL", "carry_strength": 0.8}
                ],
            },
        )[0]
        baseline_noon = closest_result_at(baseline, "12:00")
        carried_noon = closest_result_at(carried, "12:00")
        self.assertGreater(carried_noon["S"], baseline_noon["S"] + 0.5)
        self.assertLess(carried_noon["S"], baseline_noon["S"] + 8.0)
        self.assertGreater(carried_noon["cross_day_unfinished_input"], 0.0)

    def test_one_five_and_ten_minute_steps_agree(self):
        snapshots = {}
        events = EventFactory.create_from_json(self.EVENT_SET)
        for step in (1, 5, 10):
            user = User(
                params=candidate_params("m3", time_step=step),
                load_from_file=False,
            )
            rows = user.solver.simulate_day(
                events,
                50.0,
                72.0,
                "2026-07-31",
            )[0]
            snapshots[step] = [
                (
                    closest_result_at(rows, time)["S"],
                    closest_result_at(rows, time)["V"],
                    closest_result_at(rows, time)["F"],
                )
                for time in ("08:00", "12:00", "13:00", "16:00", "20:00")
            ]

        for index in range(len(snapshots[5])):
            for field in range(3):
                self.assertAlmostEqual(
                    snapshots[1][index][field],
                    snapshots[5][index][field],
                    delta=0.20,
                )
                self.assertAlmostEqual(
                    snapshots[10][index][field],
                    snapshots[5][index][field],
                    delta=0.20,
                )

    def test_online_observation_corrects_state_without_overwriting_it(self):
        events = EventFactory.create_from_json(self.EVENT_SET)
        user = User(params=candidate_params("m3"), load_from_file=False)
        baseline_rows = user.solver.simulate_day(
            events,
            50.0,
            72.0,
            "2026-07-31",
        )[0]
        observed_rows = user.solver.simulate_day(
            events,
            50.0,
            72.0,
            "2026-07-31",
            observations=[
                {
                    "time": "10:00",
                    "stress": 3.0,
                    "vitality": 8.0,
                }
            ],
        )[0]
        baseline = closest_result_at(baseline_rows, "10:00")
        corrected = closest_result_at(observed_rows, "10:00")
        self.assertLess(corrected["S"], baseline["S"])
        self.assertGreater(corrected["V"], baseline["V"])
        self.assertTrue(corrected["observation_assimilated"])
        self.assertNotEqual(corrected["S"], 30.0)

    def test_default_production_candidate_stays_at_m0(self):
        output = run_simulation_for_calibration(
            "2026-07-31",
            self.EVENT_SET,
            weave_routines=False,
        )
        self.assertEqual(output["active_states"], ["S"])
        self.assertTrue(output["model_variant"].endswith(".m0"))
        self.assertTrue(all(row["P"] == 0.0 and row["F"] == 0.0 for row in output["results"]))

    def test_ctssm_runtime_does_not_create_retired_strategy_objects(self):
        user = User(load_from_file=False)
        self.assertIsNone(user.night_strategy)
        self.assertIsNone(user.course_strategy)
        self.assertIsNone(user.rest_strategy)
        self.assertEqual(user.get_resilience_index(), 0.0)

    def test_better_than_usual_sleep_improves_cross_day_initial_state(self):
        previous = {
            "S_end": 68.0,
            "E_end": 45.0,
            "P_end": 0.3,
            "F_end": 0.5,
            "S_star": 50.0,
            "S_threshold": 70.0,
            "sleep_debt": 0.0,
        }
        poor = run_simulation_for_calibration(
            "2026-08-01",
            [],
            yesterday_state=previous,
            sleep_context={"quality_deviation": -1.0},
            weave_routines=False,
        )
        good = run_simulation_for_calibration(
            "2026-08-01",
            [],
            yesterday_state=previous,
            sleep_context={"quality_deviation": 1.0},
            weave_routines=False,
        )
        poor_first = poor["results"][0]
        good_first = good["results"][0]
        self.assertLess(good_first["S"], poor_first["S"])

    def test_sleep_only_modulates_missing_appraisal_prior(self):
        previous = {
            "S_end": 50.0,
            "E_end": 72.0,
            "S_star": 50.0,
            "S_threshold": 70.0,
            "sleep_debt": 0.0,
        }
        event = {
            "id": "unrated",
            "event_type": "task",
            "task_type": "general",
            "summary": "unrated task",
            "start_time": "09:00",
            "end_time": "11:00",
        }
        poor = run_simulation_for_calibration(
            "2026-08-01",
            [event],
            yesterday_state=previous,
            sleep_context={"quality_deviation": -1.0},
            weave_routines=False,
        )
        good = run_simulation_for_calibration(
            "2026-08-01",
            [event],
            yesterday_state=previous,
            sleep_context={"quality_deviation": 1.0},
            weave_routines=False,
        )
        self.assertGreater(
            max(row["event_stress_input"] for row in poor["results"]),
            max(row["event_stress_input"] for row in good["results"]),
        )

        explicit = {
            **event,
            "id": "rated",
            "appraisal": {"threat": 0.5, "control": 0.5},
        }
        rated_poor = run_simulation_for_calibration(
            "2026-08-01",
            [explicit],
            yesterday_state=previous,
            sleep_context={"quality_deviation": -1.0},
            weave_routines=False,
        )
        rated_good = run_simulation_for_calibration(
            "2026-08-01",
            [explicit],
            yesterday_state=previous,
            sleep_context={"quality_deviation": 1.0},
            weave_routines=False,
        )
        self.assertAlmostEqual(
            max(row["event_stress_input"] for row in rated_poor["results"]),
            max(row["event_stress_input"] for row in rated_good["results"]),
            places=8,
        )

    def test_nested_candidates_only_activate_their_declared_states(self):
        expected = {
            "m0": ["S"],
            "m1": ["S", "V"],
            "m2": ["S", "V", "P"],
            "m3": ["S", "V", "P", "F"],
        }
        for variant, states in expected.items():
            output = run_simulation_for_calibration(
                "2026-07-31",
                self.EVENT_SET,
                user_params=candidate_params(variant),
                weave_routines=False,
            )
            self.assertEqual(output["active_states"], states)

    def test_reproducible_numerical_semantics(self):
        report = run_numerical_semantic_check(GLOBAL_DEFAULT_CONFIG)
        self.assertTrue(report["passed"], report)
        self.assertEqual(
            report["evidence_type"],
            "engineering_sanity_check_not_empirical_validation",
        )


class CarePolicyTests(unittest.TestCase):
    def _rows(self, values):
        rows = []
        minute = 8 * 60
        for stress, count in values:
            for _ in range(count):
                rows.append(
                    {
                        "time": f"{minute // 60:02d}:{minute % 60:02d}",
                        "S": stress,
                        "V": 50.0,
                        "E": 50.0,
                        "P": 0.1,
                        "F": 0.45,
                        "delta_S": 0.1,
                        "state": "DAY_ACTIVE",
                        "current_events": ["任务"],
                        "dominant_stressors": ["任务"],
                        "recovery_input": 0.0,
                    }
                )
                minute += 5
        return rows

    def test_brief_non_extreme_spike_does_not_interrupt_user(self):
        rows = self._rows([(55.0, 12), (82.0, 2), (55.0, 12)])
        alerts, _ = AlertMonitor(GLOBAL_DEFAULT_CONFIG).analyze(rows)
        self.assertEqual(alerts, [])

    def test_sustained_high_stress_triggers_but_does_not_spam(self):
        rows = self._rows([(72.0, 72)])
        alerts, confidence = AlertMonitor(GLOBAL_DEFAULT_CONFIG).analyze(rows)
        self.assertGreaterEqual(len(alerts), 1)
        self.assertLessEqual(len(alerts), 2)
        self.assertEqual(len(confidence), len(rows))
        self.assertTrue(all(alert["policy"]["daily_budgeted"] for alert in alerts))

    def test_short_recovery_above_recovery_line_does_not_rearm_same_episode(self):
        rows = self._rows([(72.0, 12), (67.0, 40), (69.0, 20)])
        for row in rows[12:52]:
            row["delta_S"] = -0.2
            row["recovery_input"] = 0.5
        alerts, _ = AlertMonitor(GLOBAL_DEFAULT_CONFIG).analyze(rows)
        self.assertEqual(len(alerts), 1)
        self.assertTrue(alerts[0]["policy"]["episode_deduplicated"])

    def test_burden_alone_never_uses_red_intensity_wording(self):
        rows = self._rows([(80.0, 120)])
        alerts, _ = AlertMonitor(GLOBAL_DEFAULT_CONFIG).analyze(rows)
        self.assertTrue(alerts)
        self.assertTrue(all(alert["tier"] <= 2 for alert in alerts))
        self.assertTrue(all("[红]" not in alert["type"] for alert in alerts))

    def test_extreme_state_can_trigger_immediately(self):
        rows = self._rows([(95.0, 1)])
        alerts, _ = AlertMonitor(GLOBAL_DEFAULT_CONFIG).analyze(rows)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["tier"], 3)
        self.assertFalse(alerts[0]["policy"]["clinical_alert"])

    def test_scenario_frequency_is_restrained_but_not_silent(self):
        calm = run_simulation_for_calibration(
            "2026-07-31",
            [],
            weave_routines=False,
        )
        heavy = run_simulation_for_calibration(
            "2026-07-31",
            [
                {
                    "id": "exam",
                    "event_type": "task",
                    "task_type": "exam",
                    "summary": "考试",
                    "start_time": "08:30",
                    "end_time": "11:30",
                    "appraisal": {
                        "threat": 0.9,
                        "importance": 1.0,
                        "control": 0.2,
                        "uncertainty": 0.8,
                        "expected_effort": 0.95,
                    },
                },
                {
                    "id": "deadline",
                    "event_type": "task",
                    "task_type": "ddl",
                    "summary": "项目截止",
                    "start_time": "13:00",
                    "end_time": "18:00",
                    "objective": {"deadline": 1.0, "unfinished": 1.0},
                    "appraisal": {
                        "threat": 0.95,
                        "importance": 1.0,
                        "control": 0.1,
                        "uncertainty": 0.9,
                        "expected_effort": 1.0,
                        "rumination": 0.9,
                    },
                },
            ],
            weave_routines=False,
        )
        self.assertEqual(len(calm["alerts"]), 0)
        self.assertGreaterEqual(len(heavy["alerts"]), 1)
        self.assertLessEqual(len(heavy["alerts"]), 3)

    def test_reproducible_frequency_guardrails_pass_for_active_m0(self):
        report = run_synthetic_care_frequency_check(
            GLOBAL_DEFAULT_CONFIG,
            days=40,
        )
        self.assertTrue(report["passed"])
        self.assertEqual(
            report["evidence_type"],
            "engineering_sanity_check_not_population_validation",
        )


if __name__ == "__main__":
    unittest.main()
