"""SQLite-backed users, API keys, profiles, audit logs, and maintenance helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
from typing import Any, Dict, Optional

from werkzeug.security import check_password_hash, generate_password_hash

from settings.model_defaults import BASE_DATA_DIR


DEFAULT_APP_DB_PATH = os.path.join(BASE_DATA_DIR, "app.sqlite3")
API_KEY_PREFIX = "mhp_"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class AppDatabase:
    """Small SQLite gateway for authentication and per-user application state."""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or os.getenv("APP_DATABASE_PATH", DEFAULT_APP_DB_PATH)

    def connect(self) -> sqlite3.Connection:
        parent = os.path.dirname(os.path.abspath(self.db_path))
        os.makedirs(parent, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    def init_schema(self) -> None:
        """Create or upgrade the application schema idempotently."""
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'user'
                        CHECK(role IN ('admin', 'user')),
                    is_active INTEGER NOT NULL DEFAULT 1
                        CHECK(is_active IN (0, 1)),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_login_at TEXT
                );

                CREATE TABLE IF NOT EXISTS api_keys (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    key_prefix TEXT NOT NULL,
                    key_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    last_used_at TEXT,
                    expires_at TEXT,
                    revoked_at TEXT,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_api_keys_user
                    ON api_keys(user_id);
                CREATE INDEX IF NOT EXISTS idx_api_keys_hash
                    ON api_keys(key_hash);

                CREATE TABLE IF NOT EXISTS user_profiles (
                    user_id INTEGER PRIMARY KEY,
                    params_json TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS audit_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    action TEXT NOT NULL,
                    ip_address TEXT,
                    details_json TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE SET NULL
                );

                CREATE INDEX IF NOT EXISTS idx_audit_logs_user_created
                    ON audit_logs(user_id, created_at);
                """
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO schema_migrations(version, applied_at)
                VALUES (1, ?)
                """,
                (utc_now(),),
            )

    def create_user(self, username: str, password: str, role: str = "user") -> Dict[str, Any]:
        username = self._validate_username(username)
        self._validate_password(password)
        if not isinstance(role, str) or role not in {"admin", "user"}:
            raise ValueError("role must be 'admin' or 'user'")

        now = utc_now()
        password_hash = generate_password_hash(password)
        try:
            with self.connect() as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO users(
                        username, password_hash, role, is_active,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, 1, ?, ?)
                    """,
                    (username, password_hash, role, now, now),
                )
                user_id = int(cursor.lastrowid)
                conn.execute(
                    """
                    INSERT INTO user_profiles(user_id, params_json, updated_at)
                    VALUES (?, '{}', ?)
                    """,
                    (user_id, now),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError("username already exists") from exc
        return self.get_user(user_id)

    def authenticate_password(self, username: str, password: str) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE username = ? COLLATE NOCASE",
                ((username or "").strip(),),
            ).fetchone()
            if not row or not row["is_active"]:
                return None
            if not check_password_hash(row["password_hash"], password or ""):
                return None
            now = utc_now()
            conn.execute(
                "UPDATE users SET last_login_at = ?, updated_at = ? WHERE id = ?",
                (now, now, row["id"]),
            )
        return self.get_user(int(row["id"]))

    def get_user(self, user_id: int) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT id, username, role, is_active, created_at, updated_at,
                       last_login_at
                FROM users WHERE id = ?
                """,
                (int(user_id),),
            ).fetchone()
        return dict(row) if row else None

    def get_user_by_username(self, username: str) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT id, username, role, is_active, created_at, updated_at,
                       last_login_at
                FROM users WHERE username = ? COLLATE NOCASE
                """,
                ((username or "").strip(),),
            ).fetchone()
        return dict(row) if row else None

    def list_users(self) -> list:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, username, role, is_active, created_at, updated_at,
                       last_login_at
                FROM users ORDER BY id
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def set_user_active(self, user_id: int, is_active: bool) -> Dict[str, Any]:
        with self.connect() as conn:
            cursor = conn.execute(
                "UPDATE users SET is_active = ?, updated_at = ? WHERE id = ?",
                (1 if is_active else 0, utc_now(), int(user_id)),
            )
            if cursor.rowcount != 1:
                raise ValueError("user not found")
        return self.get_user(user_id)

    def reset_password(self, user_id: int, password: str) -> None:
        self._validate_password(password)
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE users
                SET password_hash = ?, updated_at = ?
                WHERE id = ?
                """,
                (generate_password_hash(password), utc_now(), int(user_id)),
            )
            if cursor.rowcount != 1:
                raise ValueError("user not found")

    def create_api_key(
        self,
        user_id: int,
        name: str,
        expires_days: Optional[int] = None,
    ) -> Dict[str, Any]:
        user = self.get_user(user_id)
        if not user or not user["is_active"]:
            raise ValueError("active user not found")
        if not isinstance(name, str):
            raise ValueError("API key name must be a string")
        name = name.strip()
        if not name or len(name) > 80:
            raise ValueError("API key name must contain 1-80 characters")

        raw_key = API_KEY_PREFIX + secrets.token_urlsafe(32)
        key_hash = self._hash_api_key(raw_key)
        key_prefix = raw_key[:12]
        expires_at = None
        if expires_days is not None:
            days = int(expires_days)
            if days < 1 or days > 3650:
                raise ValueError("expires_days must be within [1, 3650]")
            expires_at = (
                datetime.now(timezone.utc) + timedelta(days=days)
            ).isoformat(timespec="seconds")

        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO api_keys(
                    user_id, name, key_prefix, key_hash, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (int(user_id), name, key_prefix, key_hash, utc_now(), expires_at),
            )
            key_id = int(cursor.lastrowid)
        return {
            "id": key_id,
            "name": name,
            "key": raw_key,
            "key_prefix": key_prefix,
            "expires_at": expires_at,
        }

    def authenticate_api_key(self, raw_key: str) -> Optional[Dict[str, Any]]:
        if not raw_key or not raw_key.startswith(API_KEY_PREFIX):
            return None
        candidate_hash = self._hash_api_key(raw_key)
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT
                    k.id AS api_key_id,
                    k.key_hash,
                    k.expires_at,
                    k.revoked_at,
                    u.id,
                    u.username,
                    u.role,
                    u.is_active,
                    u.created_at,
                    u.updated_at,
                    u.last_login_at
                FROM api_keys AS k
                JOIN users AS u ON u.id = k.user_id
                WHERE k.key_hash = ?
                """,
                (candidate_hash,),
            ).fetchone()
            if not row or not row["is_active"] or row["revoked_at"]:
                return None
            if not hmac.compare_digest(row["key_hash"], candidate_hash):
                return None
            if row["expires_at"] and row["expires_at"] <= utc_now():
                return None

            cutoff = (
                datetime.now(timezone.utc) - timedelta(minutes=5)
            ).isoformat(timespec="seconds")
            conn.execute(
                """
                UPDATE api_keys
                SET last_used_at = ?
                WHERE id = ? AND (last_used_at IS NULL OR last_used_at < ?)
                """,
                (utc_now(), row["api_key_id"], cutoff),
            )

        data = dict(row)
        data.pop("key_hash", None)
        data["auth_type"] = "api_key"
        return data

    def list_api_keys(self, user_id: int) -> list:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, name, key_prefix, created_at, last_used_at,
                       expires_at, revoked_at
                FROM api_keys
                WHERE user_id = ?
                ORDER BY id DESC
                """,
                (int(user_id),),
            ).fetchall()
        return [dict(row) for row in rows]

    def revoke_api_key(self, key_id: int, user_id: Optional[int] = None) -> bool:
        clauses = ["id = ?", "revoked_at IS NULL"]
        params: list = [int(key_id)]
        if user_id is not None:
            clauses.append("user_id = ?")
            params.append(int(user_id))
        with self.connect() as conn:
            cursor = conn.execute(
                f"UPDATE api_keys SET revoked_at = ? WHERE {' AND '.join(clauses)}",
                [utc_now(), *params],
            )
        return cursor.rowcount == 1

    def load_user_params(self, user_id: int) -> Dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT params_json FROM user_profiles WHERE user_id = ?",
                (int(user_id),),
            ).fetchone()
        if not row:
            return {}
        try:
            value = json.loads(row["params_json"])
            return value if isinstance(value, dict) else {}
        except (TypeError, json.JSONDecodeError):
            return {}

    def save_user_params(self, user_id: int, params: Dict[str, Any]) -> None:
        payload = _json_dumps(params)
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO user_profiles(user_id, params_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    params_json = excluded.params_json,
                    updated_at = excluded.updated_at
                """,
                (int(user_id), payload, now),
            )

    def record_audit(
        self,
        action: str,
        user_id: Optional[int] = None,
        ip_address: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO audit_logs(
                    user_id, action, ip_address, details_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    int(user_id) if user_id is not None else None,
                    action,
                    ip_address,
                    _json_dumps(details or {}),
                    utc_now(),
                ),
            )

    def stats(self) -> Dict[str, Any]:
        self.init_schema()
        with self.connect() as conn:
            counts = {}
            for table in ("users", "api_keys", "user_profiles", "audit_logs"):
                counts[table] = int(
                    conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                )
            journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            schema_version = conn.execute(
                "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
            ).fetchone()[0]
        return {
            "path": os.path.abspath(self.db_path),
            "size_bytes": os.path.getsize(self.db_path) if os.path.exists(self.db_path) else 0,
            "journal_mode": journal_mode,
            "schema_version": int(schema_version),
            "counts": counts,
        }

    def backup(self, output_path: str) -> str:
        output_path = os.path.abspath(output_path)
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        source = self.connect()
        destination = sqlite3.connect(output_path)
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()
        return output_path

    @staticmethod
    def _hash_api_key(raw_key: str) -> str:
        return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()

    @staticmethod
    def _validate_username(username: str) -> str:
        if not isinstance(username, str):
            raise ValueError("username must be a string")
        value = username.strip()
        if len(value) < 3 or len(value) > 64:
            raise ValueError("username must contain 3-64 characters")
        if not all(char.isalnum() or char in "._-" for char in value):
            raise ValueError("username may only contain letters, numbers, '.', '_' and '-'")
        return value

    @staticmethod
    def _validate_password(password: str) -> None:
        if not isinstance(password, str):
            raise ValueError("password must be a string")
        if len(password) < 10:
            raise ValueError("password must contain at least 10 characters")
