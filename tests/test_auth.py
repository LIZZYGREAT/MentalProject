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
        user = self.database.create_user("alice", "correct-horse-battery", role="user")
        self.assertEqual(user["username"], "alice")
        self.assertIsNone(self.database.authenticate_password("alice", "wrong-password"))
        authenticated = self.database.authenticate_password(
            "alice",
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
        self.database.create_user("backup-user", "a-secure-password")
        output = TEST_DIR / "backups" / "app-backup.sqlite3"
        self.database.backup(str(output))
        backup_db = AppDatabase(str(output))
        self.assertIsNotNone(backup_db.get_user_by_username("backup-user"))


class AuthenticationApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.config.update(TESTING=True)
        existing = application_database.get_user_by_username("api-user")
        if existing is None:
            cls.user = application_database.create_user(
                "api-user",
                "api-user-password",
            )
        else:
            cls.user = existing
        admin = application_database.get_user_by_username("api-admin")
        if admin is None:
            cls.admin = application_database.create_user(
                "api-admin",
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
            json={"username": "api-user", "password": "api-user-password"},
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
            self.client.get("/api/auth/me").get_json()["user"]["username"],
            "api-user",
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
            json={"username": "api-user", "password": "invalid"},
        )
        self.assertEqual(response.status_code, 401)

    def test_malformed_login_is_rejected_without_server_error(self):
        response = self.client.post(
            "/api/auth/login",
            json={"username": 123, "password": None},
        )
        self.assertEqual(response.status_code, 401)

    def test_admin_can_manage_users_and_regular_user_cannot(self):
        admin_login = self.client.post(
            "/api/auth/login",
            json={"username": "api-admin", "password": "api-admin-password"},
        )
        self.assertEqual(admin_login.status_code, 200)
        created = self.client.post(
            "/api/admin/users",
            json={
                "username": "managed-user",
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


def tearDownModule():
    shutil.rmtree(TEST_DIR, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
