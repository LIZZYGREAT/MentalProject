from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from auth.database import AppDatabase
from integrations.feishu.identity import FeishuIdentityService
from services.care_service import CareService
from entry.app import app


class FeishuWebApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="feishu_web_api_")
        self.db_path = f"{self.temp_dir.name}/app.sqlite3"
        self.database = AppDatabase(self.db_path)
        self.database.init_schema()
        self.user = self.database.create_user(
            "web-binding@example.edu.cn", "web-binding-password"
        )
        self.identity = FeishuIdentityService(
            self.database,
            app_id="cli_test",
            bind_base_url="https://mindflow.example.edu",
        )
        self.care_service = CareService(self.database)
        app.config.update(TESTING=True)
        self.client = app.test_client()
        with self.client.session_transaction() as browser_session:
            browser_session["user_id"] = self.user["id"]
            browser_session["auth_type"] = "session"

    def tearDown(self):
        self.temp_dir.cleanup()

    def _patches(self):
        return (
            patch.dict(os.environ, {"APP_DATABASE_PATH": self.db_path}),
            patch("entry.app.application_database", self.database),
            patch("entry.app.feishu_identity_service", self.identity),
            patch("entry.app.care_service", self.care_service),
        )

    def test_confirm_status_preferences_and_revoke(self):
        issued = self.identity.create_binding_token(
            tenant_key="tenant-one",
            open_id="ou_web_user",
            chat_id="oc_web_chat",
        )
        env_patch, db_patch, identity_patch, care_patch = self._patches()
        with env_patch, db_patch, identity_patch, care_patch:
            response = self.client.post(
                "/api/feishu/bindings/confirm",
                json={"token": issued["token"]},
            )
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.get_json()["binding"]["bound"])

            status = self.client.get("/api/feishu/bindings/status")
            self.assertEqual(status.status_code, 200)
            self.assertTrue(status.get_json()["binding"]["bound"])
            self.assertNotIn("ou_web_user", str(status.get_json()))

            preferences = self.client.patch(
                "/api/care/preferences",
                json={
                    "tone": "minimal",
                    "feishu_proactive_enabled": True,
                    "allow_external_llm": False,
                },
            )
            self.assertEqual(preferences.status_code, 200)
            self.assertEqual(preferences.get_json()["preferences"]["tone"], "minimal")
            self.assertTrue(
                preferences.get_json()["preferences"]["feishu_proactive_enabled"]
            )

            revoked = self.client.delete("/api/feishu/bindings/current")
            self.assertEqual(revoked.status_code, 200)
            self.assertTrue(revoked.get_json()["revoked"])

    def test_binding_confirmation_rejects_api_key_without_browser_session(self):
        api_key = self.database.create_api_key(self.user["id"], "bot-test")["key"]
        issued = self.identity.create_binding_token(
            tenant_key="tenant-one",
            open_id="ou_api_key",
            chat_id="oc_api_key",
        )
        env_patch, db_patch, identity_patch, care_patch = self._patches()
        client = app.test_client()
        with env_patch, db_patch, identity_patch, care_patch:
            response = client.post(
                "/api/feishu/bindings/confirm",
                headers={"Authorization": f"Bearer {api_key}"},
                json={"token": issued["token"]},
            )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()["code"], "browser_session_required")


if __name__ == "__main__":
    unittest.main()
