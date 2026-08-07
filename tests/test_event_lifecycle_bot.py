from __future__ import annotations

import tempfile
import unittest
from datetime import datetime

from algorithm.dynamic_state_model import assess_event
from auth.database import AppDatabase
from services.care_agent import DeterministicCareRouter
from services.care_service import CareService
from services.care_tools import CareToolbox
from services.event_lifecycle import apply_user_appraisals, prepare_event_instances
from integrations.feishu.identity import FeishuIdentityService
from services.proactive_care import ProactiveCareScheduler
from utils.event_factory import EventFactory


class _NoRefreshPredictionService:
    def run_daily_prediction(self, user_id, target_date, **kwargs):
        return {"prediction_run_id": "unused", "local_date": target_date}


def _prediction(run_id, local_date, event):
    return {
        "prediction_run_id": run_id,
        "context_snapshot_id": None,
        "local_date": local_date,
        "schema_version": "prediction_run.v1",
        "model_version": "test",
        "parameter_version": "test",
        "feature_version": "test",
        "random_seed": 42,
        "input": {
            "schema_version": "prediction_input.v4",
            "date": local_date,
            "forecast_as_of": f"{local_date}T00:00:00",
            "events": [event],
        },
        "result": {"end_S": 50.0, "end_E": 60.0},
        "created_at": f"{local_date}T00:00:00+00:00",
        "diagnostics": {"event_profiles": []},
    }


class EventLifecycleTests(unittest.TestCase):
    def test_work_session_extracts_implicit_next_monday_obligation(self):
        event = prepare_event_instances(
            [
                {
                    "summary": "完成改作业",
                    "description": "下周一要交，请今晚完成",
                    "start_time": "19:00",
                    "end_time": "21:00",
                }
            ],
            "2026-08-09",
        )[0]
        lifecycle = event["lifecycle"]
        self.assertEqual(lifecycle["event_kind"], "work_session")
        self.assertEqual(lifecycle["completion_policy"], "work_session")
        self.assertEqual(lifecycle["obligation"]["due_at"], "2026-08-10T23:59:00")
        self.assertTrue(event["id"].startswith("calendar_"))

    def test_course_needs_no_completion_and_primary_forecast_assumes_task_completion(self):
        course, work = prepare_event_instances(
            [
                {"summary": "高数", "start_time": "08:00", "end_time": "09:40"},
                {"summary": "完成作业", "start_time": "19:00", "end_time": "21:00"},
            ],
            "2026-08-09",
        )
        self.assertEqual(course["event_type"], "course")
        self.assertEqual(course["lifecycle"]["completion_policy"], "none")
        planned = assess_event(EventFactory.create_from_json([work])[0])
        self.assertEqual(planned.objective["unfinished"], 0.0)

        incomplete = prepare_event_instances(
            [work],
            "2026-08-09",
            outcome_feedback=[
                {
                    "feedback_type": "event_completion",
                    "target_time": "2026-08-09T21:05:00+08:00",
                    "reported_at": "2026-08-09T21:05:00+08:00",
                    "payload": {
                        "event_id": work["id"],
                        "completed": False,
                        "outcome_status": "confirmed_incomplete",
                    },
                }
            ],
        )[0]
        observed = assess_event(EventFactory.create_from_json([incomplete])[0])
        self.assertGreaterEqual(observed.objective["unfinished"], 0.65)

    def test_explicit_appraisal_is_applied_to_matching_future_event(self):
        event = prepare_event_instances(
            [{"summary": "高数", "start_time": "08:00", "end_time": "09:40"}],
            "2026-08-09",
        )[0]
        enriched = apply_user_appraisals(
            [event],
            [
                {
                    "feedback_type": "event_appraisal",
                    "reported_at": "2026-08-07T10:00:00+08:00",
                    "payload": {
                        "topic": "高数",
                        "perceived_difficulty": 0.9,
                        "dislike": 0.9,
                        "threat": 0.7,
                    },
                }
            ],
        )[0]
        self.assertEqual(enriched["metadata"]["appraisal"]["threat"], 0.7)
        self.assertEqual(
            enriched["metadata"]["user_appraisal"]["source"],
            "explicit_user_feedback",
        )


class BotOutcomeTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="event_lifecycle_bot_")
        self.database = AppDatabase(f"{self.temp_dir.name}/app.sqlite3")
        self.database.init_schema()
        self.user_a = self.database.create_user("lifecycle-a@example.edu.cn", "lifecycle-password-a")
        self.user_b = self.database.create_user("lifecycle-b@example.edu.cn", "lifecycle-password-b")
        self.service = CareService(
            self.database,
            prediction_service=_NoRefreshPredictionService(),
        )
        self.tools = CareToolbox(self.service, self.user_a["id"])
        self.event = prepare_event_instances(
            [
                {
                    "summary": "完成作业",
                    "description": "下周一提交",
                    "start_time": "19:00",
                    "end_time": "21:00",
                }
            ],
            "2026-08-09",
        )[0]
        self.database.save_prediction_run(
            self.user_a["id"],
            _prediction("run-lifecycle", "2026-08-09", self.event),
            [{"time": "21:00", "S": 60.0, "E": 50.0, "state": "Awake"}],
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_bound_tool_lists_and_records_event_outcome(self):
        pending = self.tools.execute(
            "care_get_event_confirmations",
            local_date="2026-08-09",
            as_of="2026-08-09T22:00:00+08:00",
        )
        self.assertEqual(len(pending["events"]), 1)
        result = self.tools.execute(
            "care_record_event_outcome",
            prediction_run_id="run-lifecycle",
            event_id=self.event["id"],
            event_name="完成作业",
            outcome_status="confirmed_incomplete",
            observed_at="2026-08-09T21:05:00+08:00",
        )
        self.assertEqual(result["outcome_status"], "confirmed_incomplete")
        observations = self.database.list_feedback_observations(
            self.user_a["id"], target_date="2026-08-09"
        )
        self.assertFalse(observations[-1]["payload"]["completed"])
        other = CareToolbox(self.service, self.user_b["id"])
        with self.assertRaisesRegex(ValueError, "不属于当前用户"):
            other.execute(
                "care_record_event_outcome",
                prediction_run_id="run-lifecycle",
                event_id=self.event["id"],
                outcome_status="confirmed_completed",
            )

    def test_router_extracts_explicit_course_appraisal(self):
        intent = DeterministicCareRouter().route_text("我觉得高数很难")
        self.assertEqual(intent.name, "record_event_appraisal")
        self.assertEqual(intent.arguments["topic"], "高数")
        self.assertGreater(intent.arguments["perceived_difficulty"], 0.8)

    def test_proactive_scheduler_prefers_post_event_completion_check(self):
        identity = FeishuIdentityService(
            self.database,
            app_id="lifecycle-app",
            bind_base_url="https://mindflow.example.edu",
        )
        token = identity.create_binding_token(
            tenant_key="tenant",
            open_id="ou_lifecycle",
            chat_id="oc_lifecycle",
        )
        identity.confirm_binding(token["token"], self.user_a["id"])
        self.service.update_preferences(
            self.user_a["id"],
            {"feishu_proactive_enabled": True, "quiet_start": "23:00", "quiet_end": "07:00"},
        )
        candidate = ProactiveCareScheduler(
            self.database, self.service, lead_minutes=90
        ).next_candidate(datetime.fromisoformat("2026-08-09T22:00:00+08:00"))
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.kind, "completion_check")
        self.assertEqual(candidate.chat_id, "oc_lifecycle")


if __name__ == "__main__":
    unittest.main()
