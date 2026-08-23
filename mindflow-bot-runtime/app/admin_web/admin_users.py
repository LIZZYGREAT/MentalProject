"""Database authority for administrator identities and role management."""

from __future__ import annotations

from datetime import datetime, timezone
import uuid

from sqlalchemy import func, select

from app.db import Database
from app.models import AdminUser


ROLES = ("viewer", "admin", "superadmin")


def normalize_username(value: str) -> str:
    username = str(value or "").strip().casefold()
    if not 3 <= len(username) <= 128:
        raise ValueError("username must be 3-128 characters")
    return username


class AdminUserRepository:
    def __init__(self, database: Database):
        self.database = database

    def ensure_environment_superadmin(self, username: str, password_hash: str) -> AdminUser:
        """Idempotently pin the current .env identity to the highest role."""

        normalized = normalize_username(username)
        now = datetime.now(timezone.utc)
        with self.database.session() as session:
            row = session.execute(
                select(AdminUser).where(AdminUser.username == normalized)
            ).scalar_one_or_none()
            if row is None:
                row = AdminUser(
                    username=normalized,
                    password_hash=password_hash,
                    role="superadmin",
                    status="active",
                    is_environment_bootstrap=True,
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
                session.flush()
            else:
                row.password_hash = password_hash
                row.role = "superadmin"
                row.status = "active"
                row.is_environment_bootstrap = True
                row.updated_at = now
            return row

    def get_by_username(self, username: str) -> AdminUser | None:
        try:
            normalized = normalize_username(username)
        except ValueError:
            return None
        with self.database.session() as session:
            return session.execute(
                select(AdminUser).where(AdminUser.username == normalized)
            ).scalar_one_or_none()

    def get(self, admin_id: uuid.UUID | str) -> AdminUser | None:
        try:
            value = uuid.UUID(str(admin_id))
        except ValueError:
            return None
        with self.database.session() as session:
            return session.get(AdminUser, value)

    def list(self) -> list[dict]:
        with self.database.session() as session:
            rows = session.execute(
                select(AdminUser).order_by(AdminUser.created_at, AdminUser.username)
            ).scalars().all()
            return [self.public(row) for row in rows]

    def create(
        self, username: str, password_hash: str, role: str, *, created_by: uuid.UUID
    ) -> dict:
        normalized = normalize_username(username)
        if role not in ROLES:
            raise ValueError("invalid role")
        now = datetime.now(timezone.utc)
        with self.database.session() as session:
            if session.execute(
                select(AdminUser.id).where(AdminUser.username == normalized)
            ).scalar_one_or_none() is not None:
                raise ValueError("username already exists")
            row = AdminUser(
                username=normalized,
                password_hash=password_hash,
                role=role,
                status="active",
                created_by=created_by,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.flush()
            return self.public(row)

    def update(
        self,
        admin_id: uuid.UUID | str,
        *,
        role: str | None = None,
        status: str | None = None,
        password_hash: str | None = None,
        actor_id: uuid.UUID,
    ) -> dict | None:
        try:
            value = uuid.UUID(str(admin_id))
        except ValueError:
            return None
        if role is not None and role not in ROLES:
            raise ValueError("invalid role")
        if status is not None and status not in {"active", "disabled"}:
            raise ValueError("invalid status")
        with self.database.session() as session:
            row = session.get(AdminUser, value)
            if row is None:
                return None
            if row.is_environment_bootstrap and (
                (role is not None and role != "superadmin")
                or (status is not None and status != "active")
            ):
                raise ValueError("environment superadmin cannot be demoted or disabled")
            if value == actor_id and status == "disabled":
                raise ValueError("cannot disable current administrator")
            if row.role == "superadmin" and (
                role is not None and role != "superadmin" or status == "disabled"
            ):
                count = session.scalar(
                    select(func.count()).select_from(AdminUser).where(
                        AdminUser.role == "superadmin", AdminUser.status == "active"
                    )
                ) or 0
                if count <= 1:
                    raise ValueError("at least one active superadmin is required")
            if role is not None:
                row.role = role
            if status is not None:
                row.status = status
            if password_hash is not None:
                row.password_hash = password_hash
            row.updated_at = datetime.now(timezone.utc)
            session.flush()
            return self.public(row)

    def touch_login(self, admin_id: uuid.UUID) -> None:
        with self.database.session() as session:
            row = session.get(AdminUser, admin_id)
            if row:
                row.last_login_at = datetime.now(timezone.utc)

    @staticmethod
    def public(row: AdminUser) -> dict:
        return {
            "id": str(row.id),
            "username": row.username,
            "role": row.role,
            "status": row.status,
            "is_environment_bootstrap": bool(row.is_environment_bootstrap),
            "last_login_at": row.last_login_at.isoformat() if row.last_login_at else None,
            "created_at": row.created_at.isoformat(),
            "updated_at": row.updated_at.isoformat(),
        }
