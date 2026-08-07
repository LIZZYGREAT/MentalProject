from __future__ import annotations

import tempfile
import unittest

from auth.database import AppDatabase
from services.care_agent import DeterministicCareRouter
from services.care_safety import CareSafetyService
from services.care_service import CareService
from services.care_tools import CareToolbox


class FakePredictionService:
    def run_daily_prediction(self, user_id, target_date, **kwargs):
        return {
            "prediction_run_id": "fake-run",
            "local_date": target_date,
            "result": {"end_S": 70.0, "end_V": 40.0, "alerts": []},
            "calendar_connected": False,
            "calendar_degraded": True,
        }


class CareToolTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="care_tools_")
        self.database = AppDatabase(f"{self.temp_dir.name}/app.sqlite3")
        self.database.init_schema()
        self.user_a = self.database.create_user(
            "care-a@example.edu.cn", "care-password-a"
        )
        self.user_b = self.database.create_user(
            "care-b@example.edu.cn", "care-password-b"
        )
        self.service = CareService(
            self.database,
            prediction_service=FakePredictionService(),
        )
        self.tools = CareToolbox(self.service, self.user_a["id"])

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_toolbox_injects_identity_and_rejects_user_id_argument(self):
        result = self.tools.execute(
            "care_record_checkin",
            payload={
                "stress_0_10": 7,
                "vitality_0_10": 4,
                "activity": "写作业",
                "stress_event_since_last": True,
                "event_ongoing": True,
            },
        )
        self.assertEqual(result["stress_0_10"], 7)
        self.assertEqual(
            len(self.database.list_feedback_observations(self.user_a["id"])),
            1,
        )
        self.assertEqual(
            len(self.database.list_feedback_observations(self.user_b["id"])),
            0,
        )
        with self.assertRaisesRegex(ValueError, "可信运行时"):
            self.tools.execute("care_get_today_context", user_id=self.user_b["id"])
        with self.assertRaisesRegex(ValueError, "白名单"):
            self.tools.execute("read_database")

    def test_preferences_are_separate_and_proactive_defaults_off(self):
        preferences = self.tools.execute("care_update_preferences", changes={
            "tone": "minimal",
            "allow_personal_history_reference": True,
        })
        self.assertFalse(preferences["feishu_proactive_enabled"])
        self.assertTrue(preferences["allow_personal_history_reference"])
        self.assertFalse(preferences["allow_external_llm"])
        self.assertEqual(preferences["tone"], "minimal")

    def test_delivery_feedback_cannot_cross_users(self):
        delivery = self.database.create_care_delivery(
            delivery_id="delivery-a",
            user_id=self.user_a["id"],
            local_date="2026-08-02",
            episode_key="requested:event-a",
        )
        result = self.tools.execute(
            "care_submit_review",
            delivery_id=delivery["delivery_id"],
            payload={"review": "helpful"},
        )
        self.assertEqual(result["review"], "helpful")
        other_tools = CareToolbox(self.service, self.user_b["id"])
        with self.assertRaisesRegex(ValueError, "不属于当前用户"):
            other_tools.execute(
                "care_submit_review",
                delivery_id=delivery["delivery_id"],
                payload={"review": "helpful"},
            )

    def test_router_and_safety_do_not_guess_or_diagnose(self):
        router = DeterministicCareRouter()
        intent = router.route_text("我现在压力大概 7 分，活力 4 分")
        self.assertEqual(intent.name, "record_checkin")
        self.assertEqual(intent.arguments["payload"]["stress_0_10"], 7)
        safety = CareSafetyService()
        group_reply = safety.review(
            "你的压力是 7 分",
            chat_type="group",
            contains_personal_context=True,
        )
        self.assertIn("只在机器人单聊", group_reply)
        risk_reply = safety.review("", source_user_text="我不想活了")
        self.assertIn("不能替代紧急服务", risk_reply)
        diagnostic_reply = safety.review("你已经确诊为焦虑症")
        self.assertIn("不能作为医学诊断", diagnostic_reply)


if __name__ == "__main__":
    unittest.main()
