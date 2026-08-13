import asyncio
from datetime import datetime, timezone
from pathlib import Path

from app import admin
from app.bootstrap import build_business_services
from app.config import Settings
from app.integrations.feishu.oauth import DeviceFlowService
from app.main import _build_bot_transport
from app.models import FeishuDeviceFlow
from app.repositories import AgentRunRepository
from app.services.token_service import TokenEncryptionService, TokenRepository
from app.smoke.feishu_gateway import _bot_credentials
from helpers import memory_database, participant


def settings() -> Settings:
    return Settings.from_env(
        {
            "APP_ENV": "test",
            "FEISHU_BOT_APP_ID": "bot-app",
            "FEISHU_BOT_APP_SECRET": "bot-secret",
            "FEISHU_CALENDAR_APP_ID": "calendar-app",
            "FEISHU_CALENDAR_APP_SECRET": "calendar-secret",
            "DEEPSEEK_API_KEY": "key",
            "DATABASE_URL": "sqlite:///:memory:",
            "TOKEN_ENCRYPTION_KEY": TokenEncryptionService.generate_key(),
            "CLAUDE_MODEL": "deepseek-primary",
            "CLAUDE_DEFAULT_OPUS_MODEL": "deepseek-v4-pro",
            "CLAUDE_DEFAULT_SONNET_MODEL": "deepseek-v4-pro",
            "CLAUDE_DEFAULT_HAIKU_MODEL": "deepseek-v4-flash",
            "CLAUDE_CODE_SUBAGENT_MODEL": "deepseek-v4-flash",
        },
        base_dir=Path(__file__).resolve().parents[1],
    )


def test_bot_client_gateway_and_scheduler_sender_use_bot_credentials():
    seen = []

    def client_factory(app_id, app_secret):
        seen.append(("client", app_id, app_secret))
        return object()

    def gateway_factory(app_id, app_secret, *_args, **_kwargs):
        seen.append(("gateway", app_id, app_secret))
        return object()

    sender, _ = _build_bot_transport(
        settings(), object(), object(), object(),
        client_factory=client_factory,
        gateway_factory=gateway_factory,
    )

    assert seen == [
        ("client", "bot-app", "bot-secret"),
        ("gateway", "bot-app", "bot-secret"),
    ]
    # main passes this single sender object to both BotWorker and ForecastScheduler.
    assert sender is not None


def test_admin_connect_uses_calendar_credentials():
    seen = []

    class OAuth:
        def __init__(self, app_id, app_secret):
            seen.append((app_id, app_secret))
            self.app_id = app_id

    flow = admin._build_calendar_device_flow(
        memory_database(), settings(), oauth_factory=OAuth
    )

    assert seen == [("calendar-app", "calendar-secret")]
    assert flow.tokens.oauth_app_id == "calendar-app"


def test_runtime_oauth_client_and_repository_use_calendar_credentials():
    database = memory_database()
    services = build_business_services(
        database, settings(), AgentRunRepository(database)
    )

    assert services.device_flows.oauth.app_id == "calendar-app"
    assert services.device_flows.oauth.app_secret == "calendar-secret"
    assert services.token_repository.oauth_app_id == "calendar-app"


def test_gateway_smoke_uses_bot_credentials_with_legacy_fallback():
    assert _bot_credentials(
        {
            "FEISHU_BOT_APP_ID": "bot-app",
            "FEISHU_BOT_APP_SECRET": "bot-secret",
            "FEISHU_CALENDAR_APP_ID": "calendar-app",
            "FEISHU_CALENDAR_APP_SECRET": "calendar-secret",
        }
    ) == ("bot-app", "bot-secret")
    assert _bot_credentials(
        {"FEISHU_APP_ID": "legacy-app", "FEISHU_APP_SECRET": "legacy-secret"}
    ) == ("legacy-app", "legacy-secret")


class OAuth:
    def __init__(self, app_id):
        self.app_id = app_id
        self.poll_calls = 0

    async def start_device_flow(self, _scope):
        return {
            "device_code": "device-code",
            "user_code": "user-code",
            "verification_uri": "https://example.test/verify",
            "expires_in": 300,
            "interval": 1,
        }

    async def poll_device_token(self, _device_code):
        self.poll_calls += 1
        raise AssertionError("mismatched flow must not be polled")


def test_device_flow_records_oauth_app_id_and_filters_pending():
    database = memory_database()
    person = participant(database, "P001")
    encryption = TokenEncryptionService(TokenEncryptionService.generate_key())
    oauth = OAuth("calendar-app")
    service = DeviceFlowService(
        database,
        encryption,
        TokenRepository(database, encryption, oauth_app_id="calendar-app"),
        oauth,
    )

    asyncio.run(service.start(person.id))
    with database.session() as session:
        row = session.get(FeishuDeviceFlow, person.id)
        assert row.oauth_app_id == "calendar-app"
    assert service.pending_participants() == [person.id]

    with database.session() as session:
        session.get(FeishuDeviceFlow, person.id).oauth_app_id = "other-app"
    assert service.pending_participants() == []


def test_mismatched_pending_flow_is_not_polled(monkeypatch):
    database = memory_database()
    person = participant(database, "P001")
    encryption = TokenEncryptionService(TokenEncryptionService.generate_key())
    oauth = OAuth("calendar-app")
    service = DeviceFlowService(
        database,
        encryption,
        TokenRepository(database, encryption, oauth_app_id="calendar-app"),
        oauth,
    )
    asyncio.run(service.start(person.id))
    with database.session() as session:
        session.get(FeishuDeviceFlow, person.id).oauth_app_id = "other-app"

    asyncio.run(service.poll_until_complete(person.id))

    assert oauth.poll_calls == 0
    with database.session() as session:
        assert session.get(FeishuDeviceFlow, person.id).status == "failed"
