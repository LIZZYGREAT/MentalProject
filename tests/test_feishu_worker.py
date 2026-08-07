from __future__ import annotations

from urllib.parse import parse_qs, urlsplit
import tempfile
import unittest

from auth.database import AppDatabase
from integrations.feishu.client import FeishuSendError
from integrations.feishu.events import FeishuEventParser
from integrations.feishu.identity import FeishuIdentityService
from services.care_service import CareService
from services.care_worker import CareWorker
from services.feishu_message_processor import FeishuMessageProcessor


class FakePredictionService:
    def run_daily_prediction(self, user_id, target_date, **kwargs):
        return {
            "prediction_run_id": "fake-run",
            "local_date": target_date,
            "result": {"end_S": 60.0, "end_V": 55.0, "alerts": []},
            "calendar_connected": False,
            "calendar_degraded": True,
        }


class FakeBotClient:
    def __init__(self):
        self.sent = []

    def send_text(self, chat_id, text):
        self.sent.append(("text", chat_id, text))
        return f"sent-{len(self.sent)}"

    def send_card(self, chat_id, card):
        self.sent.append(("interactive", chat_id, card))
        return f"sent-{len(self.sent)}"


class FailingBotClient:
    def send_text(self, chat_id, text):
        raise FeishuSendError("temporary", code=500, retryable=True)

    def send_card(self, chat_id, card):
        raise FeishuSendError("temporary", code=500, retryable=True)


def text_event(parser, event_id, message_id, text, chat_type="p2p"):
    return parser.parse_message(
        {
            "header": {"event_id": event_id, "tenant_key": "tenant-one"},
            "event": {
                "sender": {
                    "sender_type": "user",
                    "sender_id": {"open_id": "ou_worker_user"},
                },
                "message": {
                    "message_id": message_id,
                    "chat_id": "oc_worker_chat",
                    "chat_type": chat_type,
                    "message_type": "text",
                    "content": '{"text":' + __import__("json").dumps(text, ensure_ascii=False) + '}',
                },
            },
        }
    )


class FeishuWorkerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="feishu_worker_")
        self.database = AppDatabase(f"{self.temp_dir.name}/app.sqlite3")
        self.database.init_schema()
        self.user = self.database.create_user(
            "worker@example.edu.cn", "worker-password"
        )
        self.identity = FeishuIdentityService(
            self.database,
            app_id="cli_test",
            bind_base_url="https://mindflow.example.edu",
        )
        care = CareService(
            self.database,
            prediction_service=FakePredictionService(),
        )
        self.processor = FeishuMessageProcessor(
            self.database,
            self.identity,
            care,
            web_base_url="https://mindflow.example.edu",
        )
        self.client = FakeBotClient()
        self.worker = CareWorker(
            self.database,
            self.processor,
            self.client,
            worker_id="test-worker",
            poll_seconds=0.01,
        )
        self.parser = FeishuEventParser("cli_test")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_unbound_to_bind_to_checkin_flow_and_event_idempotency(self):
        first = text_event(self.parser, "evt-bind", "msg-bind", "帮助")
        self.assertTrue(self.database.enqueue_feishu_event(first))
        self.assertTrue(self.worker.run_once())
        self.assertEqual(self.client.sent[0][0], "interactive")
        card = self.client.sent[0][2]
        bind_url = card["elements"][1]["actions"][0]["url"]
        raw_token = parse_qs(urlsplit(bind_url).query)["token"][0]
        self.identity.confirm_binding(raw_token, self.user["id"])

        checkin = text_event(
            self.parser,
            "evt-checkin",
            "msg-checkin",
            "我现在压力大概 7 分，活力 4 分",
        )
        self.assertTrue(self.database.enqueue_feishu_event(checkin))
        self.assertFalse(self.database.enqueue_feishu_event(checkin))
        self.assertTrue(self.worker.run_once())
        self.assertEqual(self.client.sent[-1][0], "text")
        self.assertIn("压力 7/10", self.client.sent[-1][2])
        observations = self.database.list_feedback_observations(self.user["id"])
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0]["feedback_type"], "momentary_state")

    def test_group_request_never_returns_personal_data(self):
        group = text_event(
            self.parser,
            "evt-group",
            "msg-group",
            "今天状态怎么样",
            chat_type="group",
        )
        self.database.enqueue_feishu_event(group)
        self.worker.run_once()
        self.assertIn("只在机器人单聊", self.client.sent[-1][2])

    def test_send_failure_retries_finitely_and_admin_metadata_is_redacted(self):
        issued = self.identity.create_binding_token(
            tenant_key="tenant-one",
            open_id="ou_worker_user",
            chat_id="oc_worker_chat",
        )
        self.identity.confirm_binding(issued["token"], self.user["id"])
        event = text_event(self.parser, "evt-fail", "msg-fail", "帮助")
        self.database.enqueue_feishu_event(event)
        worker = CareWorker(
            self.database,
            self.processor,
            FailingBotClient(),
            worker_id="failing-worker",
            poll_seconds=0.01,
            max_attempts=2,
        )
        worker.run_once()
        conn = self.database.connect()
        try:
            first = conn.execute(
                "SELECT status, attempts FROM feishu_inbox_events WHERE event_id = 'evt-fail'"
            ).fetchone()
            self.assertEqual(first["status"], "retry_wait")
            self.assertEqual(first["attempts"], 1)
            conn.execute(
                "UPDATE feishu_inbox_events SET available_at = '2000-01-01T00:00:00+00:00'"
            )
            conn.commit()
        finally:
            conn.close()
        worker.run_once()
        failures = self.database.feishu_bot_failures()
        self.assertEqual(failures[0]["status"], "dead_letter")
        self.assertEqual(failures[0]["attempts"], 2)
        self.assertNotIn("content", failures[0])
        self.assertNotIn("sender_open_id", failures[0])


if __name__ == "__main__":
    unittest.main()
