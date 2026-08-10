"""One-time invite binding; DeepSeek never participates in identity selection."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import secrets
import uuid

from sqlalchemy import select

from app.db import Database
from app.models import FeishuBinding, Participant, ParticipantInvite
from app.repositories import BindingRepository, ParticipantView


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class BindingError(ValueError):
    pass


class IdentityService:
    def __init__(self, database: Database, bindings: BindingRepository):
        self.database = database
        self.bindings = bindings

    @staticmethod
    def hash_token(raw_token: str) -> str:
        return hashlib.sha256(str(raw_token).encode("utf-8")).hexdigest()

    def create_invite(
        self, participant_id: uuid.UUID, *, ttl_seconds: int = 900
    ) -> tuple[str, datetime]:
        raw_token = secrets.token_urlsafe(24)
        expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=max(60, min(int(ttl_seconds), 86400))
        )
        with self.database.session() as session:
            participant = session.get(Participant, participant_id)
            if participant is None or participant.status != "active":
                raise BindingError("participant is not active")
            session.add(
                ParticipantInvite(
                    participant_id=participant_id,
                    token_hash=self.hash_token(raw_token),
                    expires_at=expires_at,
                )
            )
        return raw_token, expires_at

    def resolve(self, app_id: str, open_id: str) -> ParticipantView | None:
        return self.bindings.get_by_app_and_open_id(app_id, open_id)

    def bind(
        self,
        *,
        raw_token: str,
        app_id: str,
        open_id: str,
        chat_id: str,
    ) -> ParticipantView:
        if not raw_token or not app_id or not open_id or not chat_id:
            raise BindingError("binding code and Feishu identity are required")
        token_hash = self.hash_token(raw_token)
        now = datetime.now(timezone.utc)
        with self.database.session() as session:
            invite = session.execute(
                select(ParticipantInvite)
                .where(ParticipantInvite.token_hash == token_hash)
                .with_for_update()
            ).scalar_one_or_none()
            if invite is None or invite.used_at is not None or _aware(invite.expires_at) <= now:
                raise BindingError("binding code is invalid, used, or expired")
            participant = session.get(Participant, invite.participant_id, with_for_update=True)
            if participant is None or participant.status != "active":
                raise BindingError("participant is not active")

            open_binding = session.execute(
                select(FeishuBinding).where(
                    FeishuBinding.app_id == app_id,
                    FeishuBinding.open_id == open_id,
                )
            ).scalar_one_or_none()
            participant_binding = session.execute(
                select(FeishuBinding).where(
                    FeishuBinding.participant_id == participant.id
                )
            ).scalar_one_or_none()
            if open_binding and open_binding.participant_id != participant.id:
                raise BindingError("this Feishu identity is already bound")
            if participant_binding and (
                participant_binding.app_id != app_id
                or participant_binding.open_id != open_id
            ):
                raise BindingError("this participant is already bound")
            binding = open_binding or participant_binding
            if binding is None:
                session.add(
                    FeishuBinding(
                        participant_id=participant.id,
                        app_id=app_id,
                        open_id=open_id,
                        chat_id=chat_id,
                    )
                )
            else:
                binding.chat_id = chat_id
            invite.used_at = now
            return ParticipantView(
                participant.id,
                participant.participant_code,
                participant.status,
                participant.external_llm_consent_at,
            )
