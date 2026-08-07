from __future__ import annotations

from datetime import datetime, timedelta, timezone
import tempfile
import unittest

from auth.database import AppDatabase
from integrations.feishu.events import FeishuEventParser


def message_payload(event_id="evt-1", message_id="msg-1", chat_type="p2p"):
    return {
        "header": {"event_id": event_id, "tenant_key": "tenant-one"},
        "event": {
            "sender": {
                "sender_type": "user",
                "sender_id": {"open_id": "ou_sender"},
            },
            "message": {
                "message_id": message_id,
                "chat_id": "oc_chat",
                "chat_type": chat_type,
                "message_type": "text",
                "content": '{"text":"今天状态怎么样"}',
            },
        },
    }


class FeishuEventTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="feishu_events_")
        self.database = AppDatabase(f"{self.temp_dir.name}/app.sqlite3")
        self.database.init_schema()
        self.parser = FeishuEventParser("cli_test")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_message_parser_keeps_only_routing_fields(self):
        event = self.parser.parse_message(message_payload())
        self.assertEqual(event["sender_open_id"], "ou_sender")
        self.assertEqual(event["content"], {"text": "今天状态怎么样"})
        self.assertEqual(event["event_type"], "message")

    def test_card_parser_normalizes_operator_and_form_values(self):
        event = self.parser.parse_card_action(
            {
                "header": {"event_id": "card-1", "tenant_key": "tenant-one"},
                "event": {
                    "operator": {"operator_id": {"open_id": "ou_sender"}},
                    "context": {
                        "open_chat_id": "oc_chat",
                        "open_message_id": "om_card",
                    },
                    "action": {
                        "value": {"action": "care_checkin_submit"},
                        "form_value": {
                            "stress_0_10": "7",
                            "vitality_0_10": "4",
                        },
                    },
                },
            }
        )
        self.assertEqual(event["event_type"], "card_action")
        self.assertEqual(event["content"]["action"]["stress_0_10"], "7")

    def test_database_deduplicates_and_scrubs_completed_content(self):
        event = self.parser.parse_message(message_payload())
        self.assertTrue(self.database.enqueue_feishu_event(event))
        self.assertFalse(self.database.enqueue_feishu_event(event))
        duplicate_message = self.parser.parse_message(
            message_payload(event_id="evt-other", message_id="msg-1")
        )
        self.assertFalse(self.database.enqueue_feishu_event(duplicate_message))

        claimed = self.database.claim_feishu_event("worker-1")
        self.assertEqual(claimed["content"]["text"], "今天状态怎么样")
        self.assertEqual(claimed["attempts"], 1)
        self.assertTrue(self.database.complete_feishu_event(claimed["event_id"]))
        with self.database.connect() as conn:
            row = conn.execute(
                "SELECT status, content_json FROM feishu_inbox_events"
            ).fetchone()
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["content_json"], "{}")

    def test_expired_processing_lease_can_be_reclaimed(self):
        event = self.parser.parse_message(message_payload())
        self.database.enqueue_feishu_event(event)
        first = self.database.claim_feishu_event("worker-1", lease_seconds=5)
        old = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(
            timespec="seconds"
        )
        with self.database.connect() as conn:
            conn.execute(
                "UPDATE feishu_inbox_events SET claimed_at = ? WHERE event_id = ?",
                (old, first["event_id"]),
            )
        second = self.database.claim_feishu_event("worker-2", lease_seconds=5)
        self.assertEqual(second["event_id"], first["event_id"])
        self.assertEqual(second["attempts"], 2)
        self.assertEqual(second["claimed_by"], "worker-2")


if __name__ == "__main__":
    unittest.main()
