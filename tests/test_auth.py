from __future__ import annotations

import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch


TEST_DIR = Path(tempfile.mkdtemp(prefix="mental_project_auth_"))
TEST_DB = TEST_DIR / "app.sqlite3"
os.environ["APP_ENV"] = "development"
os.environ["FLASK_SECRET_KEY"] = "test-only-secret-key-with-sufficient-length"
os.environ["APP_DATABASE_PATH"] = str(TEST_DB)

from auth.database import AppDatabase  # noqa: E402
from entry.app import app, application_database  # noqa: E402


class AppDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.db_path = TEST_DIR / f"unit_{self.id().split('.')[-1]}.sqlite3"
        self.database = AppDatabase(str(self.db_path))
        self.database.init_schema()

    def test_user_password_api_key_and_profile_lifecycle(self):
        user = self.database.create_user(
            "Alice@School.edu.cn",
            "correct-horse-battery",
            role="user",
        )
        self.assertEqual(user["login_id"], "alice@school.edu.cn")
        self.assertEqual(user["login_type"], "email")
        self.assertIsNone(
            self.database.authenticate_password(
                "alice@school.edu.cn",
                "wrong-password",
            )
        )
        authenticated = self.database.authenticate_password(
            "ALICE@SCHOOL.EDU.CN",
            "correct-horse-battery",
        )
        self.assertEqual(authenticated["id"], user["id"])

        created_key = self.database.create_api_key(
            user["id"],
            "integration",
            expires_days=30,
        )
        self.assertTrue(created_key["key"].startswith("mhp_"))
        api_identity = self.database.authenticate_api_key(created_key["key"])
        self.assertEqual(api_identity["id"], user["id"])
        self.assertEqual(api_identity["auth_type"], "api_key")

        self.database.save_user_params(user["id"], {"S_star_init": 55.0})
        self.assertEqual(
            self.database.load_user_params(user["id"])["S_star_init"],
            55.0,
        )

        self.assertTrue(
            self.database.revoke_api_key(created_key["id"], user_id=user["id"])
        )
        self.assertIsNone(self.database.authenticate_api_key(created_key["key"]))

    def test_backup_is_consistent_and_readable(self):
        self.database.create_user("backup@school.edu.cn", "a-secure-password")
        output = TEST_DIR / "backups" / "app-backup.sqlite3"
        self.database.backup(str(output))
        backup_db = AppDatabase(str(output))
        self.assertIsNotNone(
            backup_db.get_user_by_login_id("backup@school.edu.cn")
        )

    def test_student_id_login_is_supported(self):
        user = self.database.create_user("2026A001", "student-password")
        self.assertEqual(user["login_type"], "student_id")
        authenticated = self.database.authenticate_password(
            "2026a001",
            "student-password",
        )
        self.assertEqual(authenticated["id"], user["id"])


class AuthenticationApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.config.update(TESTING=True)
        existing = application_database.get_user_by_login_id(
            "api-user@school.edu.cn"
        )
        if existing is None:
            cls.user = application_database.create_user(
                "api-user@school.edu.cn",
                "api-user-password",
            )
        else:
            cls.user = existing
        admin = application_database.get_user_by_login_id(
            "api-admin@school.edu.cn"
        )
        if admin is None:
            cls.admin = application_database.create_user(
                "api-admin@school.edu.cn",
                "api-admin-password",
                role="admin",
            )
        else:
            cls.admin = admin

    def setUp(self):
        self.client = app.test_client()

    def login(self):
        return self.client.post(
            "/api/auth/login",
            json={
                "login_id": "api-user@school.edu.cn",
                "password": "api-user-password",
            },
        )

    def test_health_is_public_and_config_requires_auth(self):
        self.assertEqual(self.client.get("/api/health").status_code, 200)
        response = self.client.get("/api/config")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()["code"], "authentication_required")

    def test_session_login_and_api_key_access(self):
        login_response = self.login()
        self.assertEqual(login_response.status_code, 200)
        self.assertEqual(
            self.client.get("/api/auth/me").get_json()["user"]["login_id"],
            "api-user@school.edu.cn",
        )

        key_response = self.client.post(
            "/api/auth/api-keys",
            json={"name": "test-client", "expires_days": 30},
        )
        self.assertEqual(key_response.status_code, 201)
        raw_key = key_response.get_json()["api_key"]["key"]

        self.assertEqual(self.client.post("/api/auth/logout").status_code, 200)
        api_response = self.client.get(
            "/api/config",
            headers={"Authorization": f"Bearer {raw_key}"},
        )
        self.assertEqual(api_response.status_code, 200)
        self.assertIn("params", api_response.get_json())

    def test_invalid_password_is_rejected(self):
        response = self.client.post(
            "/api/auth/login",
            json={"login_id": "api-user@school.edu.cn", "password": "invalid"},
        )
        self.assertEqual(response.status_code, 401)

    def test_feishu_connection_can_be_verified_against_primary_calendar(self):
        self.assertEqual(self.login().status_code, 200)
        with (
            patch("entry.app.FeishuAPI") as api_class,
            patch("utils.get_calendar_id.CalendarIDFetcher") as fetcher_class,
        ):
            api_class.return_value.ensure_valid_token.return_value = (
                {"access_token": "private-test-token"},
                "connected",
            )
            fetcher_class.return_value.get_calendar_info.return_value = {
                "calendar_id": "private-calendar-id",
                "summary": "课程日历",
                "role": "owner",
                "type": "primary",
            }
            response = self.client.get("/api/feishu/verify")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["valid"])
        self.assertEqual(payload["calendar"]["summary"], "课程日历")
        self.assertNotIn("calendar_id", payload["calendar"])

    def test_feishu_callback_saves_token_for_authorizing_app_user(self):
        self.assertEqual(self.login().status_code, 200)
        with self.client.session_transaction() as browser_session:
            browser_session["feishu_oauth_state"] = "expected-state"
            browser_session["feishu_oauth_user_id"] = self.user["id"]

        token_info = {
            "access_token": "private-access",
            "refresh_token": "private-refresh",
        }
        with (
            patch("entry.app.FeishuAPI") as api_class,
            patch(
                "entry.app._feishu_token_path",
                return_value="user-specific-token.json",
            ) as token_path,
        ):
            api_class.return_value.get_user_access_token.return_value = token_info
            response = self.client.get(
                "/callback?code=one-time-code&state=expected-state"
            )

        self.assertEqual(response.status_code, 200)
        token_path.assert_called_once_with(self.user["id"])
        api_class.return_value.get_user_access_token.assert_called_once_with(
            "one-time-code"
        )
        api_class.return_value.save_token_to_file.assert_called_once_with(
            token_info,
            "user-specific-token.json",
        )

    def test_manual_feishu_code_submission_is_disabled(self):
        self.assertEqual(self.login().status_code, 200)
        response = self.client.post(
            "/api/feishu/submit_code",
            json={"code": "should-not-be-accepted"},
        )
        self.assertEqual(response.status_code, 410)

    def test_malformed_login_is_rejected_without_server_error(self):
        response = self.client.post(
            "/api/auth/login",
            json={"login_id": 123, "password": None},
        )
        self.assertEqual(response.status_code, 401)

    def test_admin_can_manage_users_and_regular_user_cannot(self):
        admin_login = self.client.post(
            "/api/auth/login",
            json={
                "login_id": "api-admin@school.edu.cn",
                "password": "api-admin-password",
            },
        )
        self.assertEqual(admin_login.status_code, 200)
        created = self.client.post(
            "/api/admin/users",
            json={
                "login_id": "managed-user@school.edu.cn",
                "password": "managed-user-password",
                "role": "user",
            },
        )
        self.assertEqual(created.status_code, 201)
        self.assertEqual(
            self.client.get("/api/admin/database/stats").status_code,
            200,
        )
        self.client.post("/api/auth/logout")

        self.assertEqual(self.login().status_code, 200)
        forbidden = self.client.get("/api/admin/users")
        self.assertEqual(forbidden.status_code, 403)
        self.assertEqual(forbidden.get_json()["code"], "admin_required")

    def test_user_can_manage_only_supported_model_strategies(self):
        self.assertEqual(self.login().status_code, 200)
        current = self.client.get("/api/profile/strategies")
        self.assertEqual(current.status_code, 200)
        self.assertEqual(len(current.get_json()["strategies"]["families"]), 4)

        updated = self.client.patch(
            "/api/profile/strategies",
            json={
                "strategies": {
                    "f_strategy": "dull",
                    "C_strategy": "threshold",
                    "night_strategy": "deep",
                    "rest_strategy": "warmup",
                }
            },
        )
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(
            updated.get_json()["strategies"]["current"]["rest_strategy"],
            "warmup",
        )
        stored = application_database.load_user_params(self.user["id"])
        self.assertEqual(stored["f_strategy"], "dull")

        rejected = self.client.patch(
            "/api/profile/strategies",
            json={"strategies": {"S_star_init": 99}},
        )
        self.assertEqual(rejected.status_code, 400)

        self.client.patch(
            "/api/profile/strategies",
            json={
                "strategies": {
                    "f_strategy": "sensitive",
                    "C_strategy": "high",
                    "night_strategy": "normal",
                    "rest_strategy": "relieved",
                }
            },
        )

    def test_admin_diagnostics_expose_evidence_and_real_function_curves(self):
        response = self.client.post(
            "/api/auth/login",
            json={
                "login_id": "api-admin@school.edu.cn",
                "password": "api-admin-password",
            },
        )
        self.assertEqual(response.status_code, 200)

        overview = self.client.get("/api/admin/overview")
        self.assertEqual(overview.status_code, 200)
        self.assertIn("reliability", overview.get_json())
        self.assertIn("users", overview.get_json()["application"]["counts"])

        curves = self.client.get(
            "/api/admin/model/curves?family=rest_strategy&stress=70&energy=40"
        )
        self.assertEqual(curves.status_code, 200)
        curve_payload = curves.get_json()["curves"]
        self.assertEqual(curve_payload["family"], "rest_strategy")
        self.assertEqual(curve_payload["inputs"]["noise"], 0.0)
        self.assertIn("delta_s", curve_payload["series"][0]["points"][0])

        self.client.post("/api/auth/logout")
        self.assertEqual(self.login().status_code, 200)
        self.assertEqual(self.client.get("/api/admin/overview").status_code, 403)

    def test_api_key_can_call_simulation_endpoint(self):
        self.assertEqual(self.login().status_code, 200)
        key_response = self.client.post(
            "/api/auth/api-keys",
            json={"name": "simulation-client", "expires_days": 1},
        )
        raw_key = key_response.get_json()["api_key"]["key"]
        self.client.post("/api/auth/logout")

        with patch(
            "data_pipeline.fetcher.fetch_events_with_timeout",
            return_value=[],
        ):
            response = self.client.post(
                "/api/simulate",
                headers={"Authorization": f"Bearer {raw_key}"},
                json={
                    "date": "2026-07-07",
                    "init_S": 50,
                    "init_E": 100,
                    "mock_events": [
                        {
                            "type": "task",
                            "name": "API smoke task",
                            "start": "14:00",
                            "end": "14:30",
                            "level": "general",
                        }
                    ],
                },
            )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["status"], "success")
        self.assertIn("end_S", payload)
        stored_run = application_database.admin_prediction_run_detail(
            payload["prediction_run_id"]
        )
        self.assertIsNotNone(stored_run)
        self.assertEqual(
            stored_run["diagnostics"]["schema_version"],
            "prediction_diagnostics.v1",
        )
        self.assertGreater(len(stored_run["points"]), 0)

    def test_registration_and_versioned_onboarding_flow(self):
        response = self.client.post(
            "/api/auth/register",
            json={
                "login_id": "20260001",
                "password": "journey-user-password",
            },
        )
        self.assertEqual(response.status_code, 201)
        questionnaire = self.client.get(
            "/api/onboarding/questionnaire"
        ).get_json()["questionnaire"]
        self.assertEqual(questionnaire["schema_version"], "questionnaire_definition.v1")

        submission = self.client.post(
            "/api/onboarding/responses",
            json={
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
                    "change_experience_text": "临时变化后希望先整理任务。",
                },
            },
        )
        self.assertEqual(submission.status_code, 201)
        payload = submission.get_json()
        self.assertEqual(payload["profile"]["schema_version"], "profile_snapshot.v1")
        self.assertGreaterEqual(len(payload["profile"]["traits"]), 4)
        self.assertEqual(
            payload["routine_plan"]["schema_version"],
            "routine_plan.v1",
        )
        dashboard = self.client.get("/api/dashboard").get_json()
        self.assertTrue(dashboard["onboarding_completed"])
        self.assertIsNotNone(dashboard["routine_plan"])

    def test_phase0_replay_is_deterministic_and_does_not_mutate_profile(self):
        self.assertEqual(self.login().status_code, 200)
        user_id = application_database.get_user_by_login_id(
            "api-user@school.edu.cn"
        )["id"]
        before = application_database.load_user_params(user_id)
        request_payload = {
            "date": "2026-07-08",
            "init_S": 50,
            "init_E": 80,
            "mock_events": [
                {
                    "type": "task",
                    "name": "replay task",
                    "start": "14:00",
                    "end": "15:00",
                    "level": "ddl",
                }
            ],
        }
        with patch(
            "data_pipeline.fetcher.fetch_events_with_timeout",
            return_value=[],
        ):
            first = self.client.post("/api/simulate", json=request_payload)
            second = self.client.post("/api/simulate", json=request_payload)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        first_payload = first.get_json()
        second_payload = second.get_json()
        self.assertEqual(
            first_payload["input_fingerprint"],
            second_payload["input_fingerprint"],
        )
        self.assertAlmostEqual(first_payload["end_S"], second_payload["end_S"])
        self.assertAlmostEqual(first_payload["end_E"], second_payload["end_E"])
        self.assertNotEqual(
            first_payload["prediction_run_id"],
            second_payload["prediction_run_id"],
        )
        self.assertEqual(before, application_database.load_user_params(user_id))


def tearDownModule():
    shutil.rmtree(TEST_DIR, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
