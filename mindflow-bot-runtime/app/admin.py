"""Minimal participant provisioning utility; prints a one-time raw invite once."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from app.config import Settings
from app.db import Database, build_engine
from app.identity.service import IdentityService
from app.integrations.feishu.oauth import DeviceFlowService, FeishuOAuthClient
from app.repositories import BindingRepository, ParticipantRepository, ProfileRepository
from app.services.token_service import TokenEncryptionService, TokenRepository


def _build_calendar_device_flow(
    database: Database,
    settings: Settings,
    *,
    oauth_factory=FeishuOAuthClient,
) -> DeviceFlowService:
    encryption = TokenEncryptionService(settings.token_encryption_key)
    return DeviceFlowService(
        database,
        encryption,
        TokenRepository(
            database, encryption, oauth_app_id=settings.feishu_calendar_app_id
        ),
        oauth_factory(
            settings.feishu_calendar_app_id, settings.feishu_calendar_app_secret
        ),
    )


async def _authorize_calendar(flow: DeviceFlowService, participant_id) -> None:
    details = await flow.start(participant_id)
    print(f"verification_url={details['verification_url']}")
    print(f"user_code={details['user_code']}")
    print(f"expires_at={details['expires_at']}")
    await flow.poll_until_complete(participant_id)
    status = flow.tokens.status(participant_id)
    print(f"calendar_status={status['status']}")
    if not status.get("connected"):
        raise SystemExit("calendar authorization did not complete")


def main() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create-participant")
    create.add_argument("participant_code")
    create.add_argument("--ttl-seconds", type=int, default=900)
    connect = commands.add_parser("connect-calendar")
    connect.add_argument("participant_code")
    profile = commands.add_parser("set-profile")
    profile.add_argument("participant_code")
    profile.add_argument("json_file")
    consent = commands.add_parser("set-llm-consent")
    consent.add_argument("participant_code")
    consent.add_argument("--revoke", action="store_true")
    args = parser.parse_args()
    settings = Settings.from_env()
    database = Database(build_engine(settings.database_url))
    participants = ParticipantRepository(database)
    if args.command == "create-participant":
        participant = participants.create(args.participant_code)
        identity = IdentityService(database, BindingRepository(database))
        token, expires_at = identity.create_invite(
            participant.id, ttl_seconds=args.ttl_seconds
        )
        print(f"participant_id={participant.id}")
        print(f"bind_command=/bind {token}")
        print(f"expires_at={expires_at.isoformat()}")
        return
    participant = participants.get_by_code(args.participant_code)
    if participant is None:
        raise SystemExit("participant not found")
    if args.command == "set-profile":
        value = json.loads(Path(args.json_file).read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise SystemExit("profile JSON must be an object")
        version = ProfileRepository(database).save(participant.id, value)
        print(f"participant_id={participant.id}")
        print(f"profile_version={version}")
        return
    if args.command == "set-llm-consent":
        updated = participants.set_external_llm_consent(
            participant.id, allowed=not args.revoke
        )
        state = "revoked" if args.revoke else "granted"
        print(f"participant_id={updated.id}")
        print(f"external_llm_consent={state}")
        return
    flow = _build_calendar_device_flow(database, settings)

    asyncio.run(_authorize_calendar(flow, participant.id))


if __name__ == "__main__":
    main()
