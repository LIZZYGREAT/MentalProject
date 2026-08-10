import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.services.token_service import (
    OAuthTokenSet,
    TokenEncryptionService,
    TokenRefreshService,
    TokenRepository,
)
from app.integrations.feishu.calendar import CalendarService
from helpers import memory_database, participant


def token_set(access, refresh, *, expired=False):
    now = datetime.now(timezone.utc)
    return OAuthTokenSet(
        access_token=access,
        refresh_token=refresh,
        access_token_expires_at=now + timedelta(seconds=-10 if expired else 3600),
        refresh_token_expires_at=now + timedelta(days=7),
        granted_scopes=["calendar:calendar:readonly"],
    )


def test_tokens_are_encrypted_bound_and_isolated():
    database = memory_database()
    p1 = participant(database, "P001")
    p2 = participant(database, "P002")
    encryption = TokenEncryptionService(TokenEncryptionService.generate_key())
    repo = TokenRepository(database, encryption)
    repo.save(p1.id, token_set("access-one", "refresh-one"))
    repo.save(p2.id, token_set("access-two", "refresh-two"))

    async def should_not_refresh(_):
        raise AssertionError("valid token should not refresh")

    service = TokenRefreshService(database, encryption, should_not_refresh)
    assert asyncio.run(service.get_access_token(p1.id)) == "access-one"
    assert asyncio.run(service.get_access_token(p2.id)) == "access-two"

    encrypted = encryption.encrypt("secret", participant_id=p1.id, purpose="access")
    with pytest.raises(ValueError, match="authentication"):
        encryption.decrypt(encrypted, participant_id=p2.id, purpose="access")


def test_concurrent_refresh_performs_one_rotation():
    database = memory_database()
    p1 = participant(database, "P001")
    encryption = TokenEncryptionService(TokenEncryptionService.generate_key())
    repo = TokenRepository(database, encryption)
    repo.save(p1.id, token_set("expired", "refresh-old", expired=True))
    refresh_count = 0

    async def refresh(value):
        nonlocal refresh_count
        assert value == "refresh-old"
        refresh_count += 1
        await asyncio.sleep(0.01)
        return token_set("access-new", "refresh-new")

    service = TokenRefreshService(database, encryption, refresh)

    async def run_both():
        return await asyncio.gather(
            service.get_access_token(p1.id), service.get_access_token(p1.id)
        )

    assert asyncio.run(run_both()) == ["access-new", "access-new"]
    assert refresh_count == 1
    assert repo.status(p1.id)["token_version"] == 2


def test_calendar_resolves_the_requested_participant_token(monkeypatch):
    database = memory_database()
    p1 = participant(database, "P001")
    p2 = participant(database, "P002")

    class Tokens:
        def __init__(self):
            self.calls = []

        async def get_access_token(self, participant_id):
            self.calls.append(participant_id)
            return {p1.id: "token-one", p2.id: "token-two"}[participant_id]

    authorization_headers = []

    class Response:
        status_code = 200

        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, _url, *, headers, params):
            authorization_headers.append(headers["Authorization"])
            return Response(
                {
                    "code": 0,
                    "data": {
                        "calendars": [
                            {"calendar": {"calendar_id": "primary", "type": "primary"}}
                        ]
                    },
                }
            )

        async def get(self, _url, *, headers, params):
            assert _url.endswith("/events/instance_view")
            authorization_headers.append(headers["Authorization"])
            return Response({"code": 0, "data": {"items": []}})

    monkeypatch.setattr("app.integrations.feishu.calendar.httpx.AsyncClient", Client)
    tokens = Tokens()
    calendar = CalendarService(tokens)
    now = datetime.now(timezone.utc)

    async def read_both():
        await calendar.get_events(p1.id, now, now + timedelta(hours=1))
        await calendar.get_events(p2.id, now, now + timedelta(hours=1))

    asyncio.run(read_both())
    assert tokens.calls == [p1.id, p2.id]
    assert authorization_headers == [
        "Bearer token-one",
        "Bearer token-one",
        "Bearer token-two",
        "Bearer token-two",
    ]
