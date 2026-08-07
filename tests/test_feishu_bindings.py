from __future__ import annotations

import tempfile
import unittest

from auth.database import AppDatabase
from integrations.feishu.identity import FeishuIdentityService


class FeishuBindingTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="feishu_bindings_")
        self.database = AppDatabase(f"{self.temp_dir.name}/app.sqlite3")
        self.database.init_schema()
        self.user_a = self.database.create_user(
            "binding-a@example.edu.cn", "binding-password-a"
        )
        self.user_b = self.database.create_user(
            "binding-b@example.edu.cn", "binding-password-b"
        )
        self.identity = FeishuIdentityService(
            self.database,
            app_id="cli_test",
            bind_base_url="https://mindflow.example.edu",
            token_ttl_seconds=900,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_token_is_hashed_one_time_and_binding_is_user_isolated(self):
        issued = self.identity.create_binding_token(
            tenant_key="tenant-one",
            open_id="ou_binding_a",
            chat_id="oc_private_a",
        )
        self.assertIn("/feishu/bind?token=", issued["bind_url"])
        with self.database.connect() as conn:
            row = conn.execute(
                "SELECT token_hash FROM bot_binding_tokens"
            ).fetchone()
            self.assertNotEqual(row["token_hash"], issued["token"])
            self.assertNotIn(
                issued["token"],
                str(conn.execute("SELECT * FROM bot_binding_tokens").fetchall()),
            )

        binding = self.identity.confirm_binding(issued["token"], self.user_a["id"])
        self.assertEqual(binding["user_id"], self.user_a["id"])
        resolved = self.identity.resolve_binding("tenant-one", "ou_binding_a")
        self.assertEqual(resolved["user_id"], self.user_a["id"])
        self.assertTrue(resolved["user_is_active"])

        with self.assertRaisesRegex(ValueError, "已使用|已过期"):
            self.identity.confirm_binding(issued["token"], self.user_a["id"])

        other_user_token = self.identity.create_binding_token(
            tenant_key="tenant-one",
            open_id="ou_binding_a",
            chat_id="oc_private_a",
        )
        with self.assertRaisesRegex(ValueError, "已绑定其他"):
            self.identity.confirm_binding(
                other_user_token["token"], self.user_b["id"]
            )

    def test_one_project_user_cannot_bind_two_active_feishu_identities(self):
        first = self.identity.create_binding_token(
            tenant_key="tenant-one",
            open_id="ou_first",
            chat_id="oc_first",
        )
        self.identity.confirm_binding(first["token"], self.user_a["id"])
        second = self.identity.create_binding_token(
            tenant_key="tenant-one",
            open_id="ou_second",
            chat_id="oc_second",
        )
        with self.assertRaisesRegex(ValueError, "已绑定其他飞书账号"):
            self.identity.confirm_binding(second["token"], self.user_a["id"])

    def test_new_binding_link_invalidates_older_pending_link_for_same_sender(self):
        old = self.identity.create_binding_token(
            tenant_key="tenant-one",
            open_id="ou_pending",
            chat_id="oc_pending",
        )
        current = self.identity.create_binding_token(
            tenant_key="tenant-one",
            open_id="ou_pending",
            chat_id="oc_pending",
        )
        with self.assertRaisesRegex(ValueError, "无效|已使用|已过期"):
            self.identity.confirm_binding(old["token"], self.user_a["id"])
        binding = self.identity.confirm_binding(current["token"], self.user_a["id"])
        self.assertEqual(binding["open_id"], "ou_pending")

    def test_revoke_and_inactive_user_take_effect_immediately(self):
        issued = self.identity.create_binding_token(
            tenant_key="tenant-one",
            open_id="ou_revoke",
            chat_id="oc_revoke",
        )
        self.identity.confirm_binding(issued["token"], self.user_a["id"])
        self.database.set_user_active(self.user_a["id"], False)
        resolved = self.identity.resolve_binding("tenant-one", "ou_revoke")
        self.assertFalse(resolved["user_is_active"])
        self.assertTrue(self.identity.revoke_binding(self.user_a["id"]))
        self.assertIsNone(self.identity.resolve_binding("tenant-one", "ou_revoke"))
        self.assertFalse(self.identity.status_for_user(self.user_a["id"])["bound"])


if __name__ == "__main__":
    unittest.main()
