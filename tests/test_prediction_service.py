from __future__ import annotations

from datetime import date
import tempfile
import unittest

from auth.database import AppDatabase
from services.onboarding import (
    QUESTIONNAIRE_DEFINITION,
    build_daily_context,
    build_routine_plan,
    infer_profile,
    validate_and_normalize_submission,
)
from services.prediction_service import PredictionService


class FailingCalendarApi:
    def ensure_valid_token(self, path):
        raise RuntimeError("calendar unavailable")


class PredictionServiceIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="prediction_service_")
        self.database = AppDatabase(f"{self.temp_dir.name}/app.sqlite3")
        self.database.init_schema()
        self.database.save_questionnaire_definition(QUESTIONNAIRE_DEFINITION)
        self.user = self.database.create_user(
            "prediction-service@example.edu.cn", "prediction-service-password"
        )
        response = validate_and_normalize_submission(
            {
                "timezone": "Asia/Shanghai",
                "answers": {
                    "weekday_sleep_start": "23:20",
                    "weekday_wake_time": "07:20",
                    "lunch_ideal_time": "12:10",
                    "dinner_ideal_time": "18:20",
                    "nap_frequency": "sometimes",
                    "stress_change_01": 4,
                    "adapt_change_reverse_01": 3,
                    "recovery_speed_01": 4,
                    "continuous_load_01": 4,
                    "morning_energy_reverse_01": 3,
                    "social_evaluation_01": 5,
                    "support_style": ["task_breakdown", "short_break"],
                    "care_tone": "brief_warm",
                    "change_experience_text": "",
                },
            }
        )
        inference, profile = infer_profile(response)
        plan = build_routine_plan(profile, target_date=date.today().isoformat())
        context = build_daily_context(profile, plan, date.today().isoformat())
        self.database.save_onboarding_bundle(
            self.user["id"], response, inference, profile, plan, context
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_daily_prediction_uses_existing_model_and_degrades_without_calendar(self):
        service = PredictionService(
            self.database,
            token_path_factory=lambda user_id: "unused-token-path",
            feishu_api_factory=lambda: FailingCalendarApi(),
        )
        result = service.run_daily_prediction(self.user["id"], "2026-08-03")
        self.assertTrue(result["calendar_degraded"])
        self.assertFalse(result["calendar_connected"])
        self.assertIn("end_S", result["result"])
        stored = self.database.latest_prediction_run_for_date(
            self.user["id"], "2026-08-03"
        )
        self.assertEqual(stored["prediction_run_id"], result["prediction_run_id"])
        self.assertGreater(len(stored["points"]), 0)


if __name__ == "__main__":
    unittest.main()
