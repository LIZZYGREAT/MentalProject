from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from data_pipeline import fetcher
from utils.get_calendar_id import CalendarIDFetcher
from utils.get_token import FeishuAPI, FeishuAPIError


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class FeishuTokenTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="mental_feishu_")
        self.token_path = Path(self.temp_dir.name) / "user_1.json"
        self.api = FeishuAPI(
            app_id="cli_test",
            app_secret="test-secret",
            redirect_uri="http://127.0.0.1:5000/callback",
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_authorize_url_requests_offline_and_calendar_scopes(self):
        url = self.api.generate_authorize_url(state="safe-state")
        query = parse_qs(urlparse(url).query)
        scopes = set(query["scope"][0].split())
        self.assertIn("offline_access", scopes)
        self.assertIn("calendar:calendar:readonly", scopes)
        self.assertEqual(query["state"], ["safe-state"])

    def test_api_explorer_cannot_be_used_as_oauth_callback(self):
        with self.assertRaisesRegex(ValueError, "不能指向飞书 API 调试台"):
            FeishuAPI(
                app_id="cli_test",
                app_secret="test-secret",
                redirect_uri="https://open.feishu.cn/api-explorer/loading",
            )

    def test_production_cannot_use_a_loopback_oauth_callback(self):
        with (
            patch.dict(os.environ, {"APP_ENV": "production"}),
            self.assertRaisesRegex(ValueError, "公网 HTTPS 域名"),
        ):
            FeishuAPI(
                app_id="cli_test",
                app_secret="test-secret",
                redirect_uri="http://127.0.0.1:5000/callback",
            )

    def test_v2_token_exchange_normalizes_refresh_expiry(self):
        payload = {
            "code": 0,
            "access_token": "access-one",
            "expires_in": 7200,
            "refresh_token": "refresh-one",
            "refresh_token_expires_in": 604800,
            "scope": "offline_access calendar:calendar:readonly",
            "token_type": "Bearer",
        }
        with patch(
            "utils.get_token.requests.post",
            return_value=_Response(payload),
        ):
            token = self.api.get_user_access_token("one-time-code")

        self.assertEqual(token["access_token"], "access-one")
        self.assertEqual(token["refresh_token"], "refresh-one")
        self.assertEqual(token["refresh_token_expires_in"], 604800)
        self.assertGreater(token["expires_at"], token["timestamp"])

    def test_expired_access_token_is_refreshed_and_rotated_once(self):
        old_token = {
            "access_token": "expired-access",
            "expires_in": 60,
            "refresh_token": "refresh-old",
            "refresh_token_expires_in": 604800,
            "scope": "offline_access calendar:calendar:readonly",
            "timestamp": int(time.time()) - 3600,
        }
        self.api.save_token_to_file(old_token, self.token_path)
        refreshed = {
            "access_token": "access-new",
            "expires_in": 7200,
            "refresh_token": "refresh-new",
            "refresh_token_expires_in": 604800,
            "scope": "offline_access calendar:calendar:readonly",
            "timestamp": int(time.time()),
            "expires_at": int(time.time()) + 7200,
            "refresh_token_expires_at": int(time.time()) + 604800,
            "token_type": "Bearer",
        }

        with patch.object(
            self.api,
            "refresh_user_access_token",
            return_value=refreshed,
        ) as refresh:
            token, state = self.api.ensure_valid_token(self.token_path)
            second_token, second_state = self.api.ensure_valid_token(self.token_path)

        self.assertEqual(state, "refreshed")
        self.assertEqual(second_state, "connected")
        self.assertEqual(token["refresh_token"], "refresh-new")
        self.assertEqual(second_token["access_token"], "access-new")
        refresh.assert_called_once_with("refresh-old")
        saved = json.loads(self.token_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["refresh_token"], "refresh-new")

    def test_v2_refresh_request_rotates_the_one_time_refresh_token(self):
        payload = {
            "code": 0,
            "access_token": "access-new",
            "expires_in": 7200,
            "refresh_token": "refresh-new",
            "refresh_token_expires_in": 604800,
            "scope": "offline_access calendar:calendar:readonly",
            "token_type": "Bearer",
        }
        with patch(
            "utils.get_token.requests.post",
            return_value=_Response(payload),
        ) as post:
            token = self.api.refresh_user_access_token("refresh-old")

        post.assert_called_once_with(
            "https://open.feishu.cn/open-apis/authen/v2/oauth/token",
            json={
                "grant_type": "refresh_token",
                "client_id": "cli_test",
                "client_secret": "test-secret",
                "refresh_token": "refresh-old",
            },
            timeout=15.0,
        )
        self.assertEqual(token["access_token"], "access-new")
        self.assertEqual(token["refresh_token"], "refresh-new")

    def test_used_refresh_token_requires_reauthorization(self):
        expired_token = {
            "access_token": "expired-access",
            "expires_in": 60,
            "refresh_token": "already-used-refresh",
            "refresh_token_expires_in": 604800,
            "timestamp": int(time.time()) - 3600,
        }
        self.api.save_token_to_file(expired_token, self.token_path)
        with patch.object(
            self.api,
            "refresh_user_access_token",
            side_effect=FeishuAPIError("refresh token 已使用", 20073),
        ):
            status = self.api.get_connection_status(self.token_path)

        self.assertEqual(status["status"], "reauthorization_required")
        self.assertTrue(status["needs_reauthorization"])
        self.assertEqual(status["provider_error_code"], 20073)

    def test_connection_status_never_exposes_tokens(self):
        token = {
            "access_token": "private-access",
            "expires_in": 7200,
            "refresh_token": "private-refresh",
            "refresh_token_expires_in": 604800,
            "scope": "offline_access calendar:calendar:readonly",
            "timestamp": int(time.time()),
        }
        self.api.save_token_to_file(token, self.token_path)
        status = self.api.get_connection_status(self.token_path)

        self.assertTrue(status["valid"])
        self.assertTrue(status["refreshable"])
        self.assertNotIn("access_token", status)
        self.assertNotIn("refresh_token", status)

    def test_calendar_cache_is_isolated_by_application_user(self):
        fetcher._TTL_CACHE.clear()

        def fake_fetch(date_str, open_id, injected_token, injected_calendar_id):
            return [{"date": date_str, "token_marker": injected_token}]

        with patch.object(
            fetcher,
            "fetch_events_from_calendar_internal",
            side_effect=fake_fetch,
        ) as request_events:
            first = fetcher.fetch_events_with_timeout(
                "2026-07-30",
                injected_token="token-user-1",
                cache_namespace="user:1",
            )
            second = fetcher.fetch_events_with_timeout(
                "2026-07-30",
                injected_token="token-user-2",
                cache_namespace="user:2",
            )
            first_cached = fetcher.fetch_events_with_timeout(
                "2026-07-30",
                injected_token="token-user-1",
                cache_namespace="user:1",
            )

        self.assertEqual(first[0]["token_marker"], "token-user-1")
        self.assertEqual(second[0]["token_marker"], "token-user-2")
        self.assertEqual(first_cached, first)
        self.assertEqual(request_events.call_count, 2)

    def test_web_user_token_resolves_its_own_primary_calendar(self):
        with (
            patch.dict(os.environ, {"FEISHU_CALENDAR_ID": "legacy-global-calendar"}),
            patch("utils.get_calendar_id.CalendarIDFetcher") as fetcher_class,
        ):
            fetcher_class.return_value.get_calendar_info.return_value = {
                "calendar_id": "current-user-primary",
                "owner_id": "current-user",
            }
            calendar_id = fetcher._resolve_calendar_id(
                injected_token="current-user-token"
            )

        self.assertEqual(calendar_id, "current-user-primary")
        fetcher_class.return_value.get_calendar_info.assert_called_once_with(
            "current-user-token"
        )

    def test_primary_calendar_uses_documented_post_method(self):
        primary_response = {
            "code": 0,
            "msg": "success",
            "data": {
                "calendars": [
                    {
                        "calendar": {
                            "calendar_id": "current-user-primary",
                            "summary": "我的日历",
                            "type": "primary",
                            "role": "owner",
                        },
                        "user_id": "ou_current_user",
                    }
                ]
            },
        }
        calendar_fetcher = CalendarIDFetcher.__new__(CalendarIDFetcher)
        with (
            patch(
                "utils.get_calendar_id.requests.post",
                return_value=_Response(primary_response),
            ) as post,
            patch("utils.get_calendar_id.requests.get") as get,
        ):
            calendar = calendar_fetcher.get_calendar_info("current-user-token")

        self.assertEqual(calendar["calendar_id"], "current-user-primary")
        self.assertEqual(calendar["owner_id"], "ou_current_user")
        post.assert_called_once()
        get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
