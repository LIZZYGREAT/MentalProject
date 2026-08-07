"""SQLite-backed users, API keys, profiles, audit logs, and maintenance helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
from typing import Any, Dict, Optional

from werkzeug.security import check_password_hash, generate_password_hash

from settings.model_defaults import BASE_DATA_DIR


DEFAULT_APP_DB_PATH = os.path.join(BASE_DATA_DIR, "app.sqlite3")
API_KEY_PREFIX = "mhp_"
EMAIL_PATTERN = re.compile(
    r"^[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?"
    r"(?:\.[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?)+$",
    re.IGNORECASE,
)
STUDENT_ID_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9_-]{4,31}$", re.IGNORECASE)


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
            self._upgrade_empty_legacy_users_table(conn)
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
                    login_id TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    login_type TEXT NOT NULL
                        CHECK(login_type IN ('email', 'student_id')),
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

                CREATE TABLE IF NOT EXISTS questionnaire_definitions (
                    questionnaire_version TEXT PRIMARY KEY,
                    schema_version TEXT NOT NULL,
                    definition_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS questionnaire_responses (
                    response_id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    questionnaire_version TEXT NOT NULL,
                    schema_version TEXT NOT NULL,
                    timezone TEXT NOT NULL,
                    answers_json TEXT NOT NULL,
                    submitted_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                    FOREIGN KEY(questionnaire_version)
                        REFERENCES questionnaire_definitions(questionnaire_version)
                );

                CREATE INDEX IF NOT EXISTS idx_questionnaire_responses_user
                    ON questionnaire_responses(user_id, submitted_at DESC);

                CREATE TABLE IF NOT EXISTS profile_inference_runs (
                    profile_inference_run_id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    response_id TEXT NOT NULL,
                    mapping_version TEXT NOT NULL,
                    schema_version TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                    FOREIGN KEY(response_id)
                        REFERENCES questionnaire_responses(response_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS profile_snapshots (
                    profile_snapshot_id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    profile_inference_run_id TEXT NOT NULL,
                    mapping_version TEXT NOT NULL,
                    schema_version TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                    FOREIGN KEY(profile_inference_run_id)
                        REFERENCES profile_inference_runs(profile_inference_run_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_profile_snapshots_user
                    ON profile_snapshots(user_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS routine_plans (
                    routine_plan_id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    local_date TEXT NOT NULL,
                    profile_snapshot_id TEXT NOT NULL,
                    schema_version TEXT NOT NULL,
                    rule_version TEXT NOT NULL,
                    plan_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                    FOREIGN KEY(profile_snapshot_id)
                        REFERENCES profile_snapshots(profile_snapshot_id)
                );

                CREATE INDEX IF NOT EXISTS idx_routine_plans_user_date
                    ON routine_plans(user_id, local_date, created_at DESC);

                CREATE TABLE IF NOT EXISTS daily_context_snapshots (
                    context_snapshot_id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    target_date TEXT NOT NULL,
                    profile_snapshot_id TEXT NOT NULL,
                    routine_plan_id TEXT NOT NULL,
                    schema_version TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                    FOREIGN KEY(profile_snapshot_id)
                        REFERENCES profile_snapshots(profile_snapshot_id),
                    FOREIGN KEY(routine_plan_id)
                        REFERENCES routine_plans(routine_plan_id)
                );

                CREATE TABLE IF NOT EXISTS prediction_runs (
                    prediction_run_id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    context_snapshot_id TEXT,
                    local_date TEXT NOT NULL,
                    schema_version TEXT NOT NULL,
                    model_version TEXT NOT NULL,
                    parameter_version TEXT NOT NULL,
                    feature_version TEXT NOT NULL,
                    random_seed INTEGER NOT NULL,
                    input_json TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                    FOREIGN KEY(context_snapshot_id)
                        REFERENCES daily_context_snapshots(context_snapshot_id)
                );

                CREATE INDEX IF NOT EXISTS idx_prediction_runs_user_date
                    ON prediction_runs(user_id, local_date, created_at DESC);

                CREATE TABLE IF NOT EXISTS state_points (
                    prediction_run_id TEXT NOT NULL,
                    point_index INTEGER NOT NULL,
                    local_time TEXT NOT NULL,
                    stress REAL NOT NULL,
                    energy REAL NOT NULL,
                    state TEXT NOT NULL,
                    point_json TEXT NOT NULL,
                    PRIMARY KEY(prediction_run_id, point_index),
                    FOREIGN KEY(prediction_run_id)
                        REFERENCES prediction_runs(prediction_run_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS prediction_diagnostics (
                    prediction_run_id TEXT PRIMARY KEY,
                    diagnostics_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(prediction_run_id)
                        REFERENCES prediction_runs(prediction_run_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS feedback_observations (
                    feedback_id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    prediction_run_id TEXT,
                    feedback_type TEXT NOT NULL,
                    schema_version TEXT NOT NULL,
                    target_time TEXT,
                    payload_json TEXT NOT NULL,
                    reported_at TEXT NOT NULL,
                    retrospective INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                    FOREIGN KEY(prediction_run_id)
                        REFERENCES prediction_runs(prediction_run_id) ON DELETE SET NULL
                );

                CREATE INDEX IF NOT EXISTS idx_feedback_observations_user
                    ON feedback_observations(user_id, reported_at DESC);

                CREATE TABLE IF NOT EXISTS feishu_user_bindings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    app_id TEXT NOT NULL,
                    tenant_key TEXT NOT NULL DEFAULT '',
                    open_id TEXT NOT NULL,
                    chat_id TEXT,
                    binding_status TEXT NOT NULL DEFAULT 'active'
                        CHECK(binding_status IN ('active', 'revoked')),
                    bound_at TEXT NOT NULL,
                    revoked_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                );

                CREATE UNIQUE INDEX IF NOT EXISTS uq_feishu_binding_open_active
                    ON feishu_user_bindings(app_id, open_id)
                    WHERE binding_status = 'active';
                CREATE UNIQUE INDEX IF NOT EXISTS uq_feishu_binding_user_active
                    ON feishu_user_bindings(app_id, user_id)
                    WHERE binding_status = 'active';
                CREATE INDEX IF NOT EXISTS idx_feishu_bindings_user
                    ON feishu_user_bindings(user_id, updated_at DESC);

                CREATE TABLE IF NOT EXISTS bot_binding_tokens (
                    token_id TEXT PRIMARY KEY,
                    token_hash TEXT NOT NULL UNIQUE,
                    app_id TEXT NOT NULL,
                    tenant_key TEXT NOT NULL DEFAULT '',
                    open_id TEXT NOT NULL,
                    chat_id TEXT,
                    expires_at TEXT NOT NULL,
                    used_at TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_bot_binding_tokens_lookup
                    ON bot_binding_tokens(token_hash, expires_at);

                CREATE TABLE IF NOT EXISTS feishu_inbox_events (
                    event_id TEXT PRIMARY KEY,
                    message_id TEXT,
                    app_id TEXT NOT NULL,
                    tenant_key TEXT NOT NULL DEFAULT '',
                    sender_open_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    chat_type TEXT NOT NULL,
                    message_type TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    content_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'received'
                        CHECK(status IN (
                            'received', 'processing', 'completed', 'retry_wait',
                            'failed', 'dead_letter', 'ignored'
                        )),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    available_at TEXT NOT NULL,
                    claimed_at TEXT,
                    claimed_by TEXT,
                    completed_at TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE UNIQUE INDEX IF NOT EXISTS uq_feishu_inbox_message
                    ON feishu_inbox_events(app_id, message_id)
                    WHERE message_id IS NOT NULL AND message_id != '';
                CREATE INDEX IF NOT EXISTS idx_feishu_inbox_claim
                    ON feishu_inbox_events(status, available_at, created_at);

                CREATE TABLE IF NOT EXISTS care_deliveries (
                    delivery_id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    prediction_run_id TEXT,
                    local_date TEXT NOT NULL,
                    alert_time TEXT,
                    tier TEXT NOT NULL DEFAULT 'support',
                    episode_key TEXT NOT NULL,
                    channel TEXT NOT NULL DEFAULT 'feishu',
                    status TEXT NOT NULL DEFAULT 'candidate'
                        CHECK(status IN (
                            'candidate', 'scheduled', 'sending', 'sent',
                            'retry_wait', 'failed', 'suppressed'
                        )),
                    scheduled_at TEXT,
                    sent_at TEXT,
                    provider_message_id TEXT,
                    failure_reason TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                    FOREIGN KEY(prediction_run_id)
                        REFERENCES prediction_runs(prediction_run_id) ON DELETE SET NULL,
                    UNIQUE(user_id, local_date, episode_key, channel)
                );

                CREATE INDEX IF NOT EXISTS idx_care_deliveries_user_date
                    ON care_deliveries(user_id, local_date, status);

                CREATE TABLE IF NOT EXISTS care_channel_preferences (
                    user_id INTEGER PRIMARY KEY,
                    feishu_proactive_enabled INTEGER NOT NULL DEFAULT 0
                        CHECK(feishu_proactive_enabled IN (0, 1)),
                    quiet_start TEXT NOT NULL DEFAULT '23:00',
                    quiet_end TEXT NOT NULL DEFAULT '07:00',
                    max_daily_messages INTEGER NOT NULL DEFAULT 2,
                    tone TEXT NOT NULL DEFAULT 'supportive',
                    preferred_support_json TEXT NOT NULL DEFAULT '[]',
                    allow_personal_history_reference INTEGER NOT NULL DEFAULT 0
                        CHECK(allow_personal_history_reference IN (0, 1)),
                    allow_external_llm INTEGER NOT NULL DEFAULT 0
                        CHECK(allow_external_llm IN (0, 1)),
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS bot_runtime_heartbeats (
                    process_name TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    details_json TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL
                );
                """
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO schema_migrations(version, applied_at)
                VALUES (1, ?)
                """,
                (utc_now(),),
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO schema_migrations(version, applied_at)
                VALUES (2, ?)
                """,
                (utc_now(),),
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO schema_migrations(version, applied_at)
                VALUES (3, ?)
                """,
                (utc_now(),),
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO schema_migrations(version, applied_at)
                VALUES (4, ?)
                """,
                (utc_now(),),
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO schema_migrations(version, applied_at)
                VALUES (5, ?)
                """,
                (utc_now(),),
            )

    @staticmethod
    def _upgrade_empty_legacy_users_table(conn: sqlite3.Connection) -> None:
        """Replace the legacy username table after its users have been cleared."""
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(users)").fetchall()
        }
        if not columns or "login_id" in columns:
            return
        if "username" not in columns:
            raise RuntimeError("users 表结构无法识别，已停止自动迁移")
        user_count = int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])
        if user_count:
            raise RuntimeError(
                "旧用户名账号仍存在；请先导出或清空用户后再升级邮箱/学号登录"
            )

        conn.commit()
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            conn.executescript(
                """
                CREATE TABLE users_v3 (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    login_id TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    login_type TEXT NOT NULL
                        CHECK(login_type IN ('email', 'student_id')),
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'user'
                        CHECK(role IN ('admin', 'user')),
                    is_active INTEGER NOT NULL DEFAULT 1
                        CHECK(is_active IN (0, 1)),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_login_at TEXT
                );
                DROP TABLE users;
                ALTER TABLE users_v3 RENAME TO users;
                """
            )
        finally:
            conn.execute("PRAGMA foreign_keys = ON")

    def create_user(self, login_id: str, password: str, role: str = "user") -> Dict[str, Any]:
        login_id, login_type = self._validate_login_id(login_id)
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
                        login_id, login_type, password_hash, role, is_active,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 1, ?, ?)
                    """,
                    (login_id, login_type, password_hash, role, now, now),
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
            raise ValueError("该邮箱或学号已注册") from exc
        return self.get_user(user_id)

    def authenticate_password(self, login_id: str, password: str) -> Optional[Dict[str, Any]]:
        try:
            normalized_login_id, _ = self._validate_login_id(login_id)
        except ValueError:
            return None
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE login_id = ? COLLATE NOCASE",
                (normalized_login_id,),
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
                SELECT id, login_id, login_type, role, is_active, created_at, updated_at,
                       last_login_at
                FROM users WHERE id = ?
                """,
                (int(user_id),),
            ).fetchone()
        return dict(row) if row else None

    def get_user_by_login_id(self, login_id: str) -> Optional[Dict[str, Any]]:
        try:
            normalized_login_id, _ = self._validate_login_id(login_id)
        except ValueError:
            return None
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT id, login_id, login_type, role, is_active, created_at, updated_at,
                       last_login_at
                FROM users WHERE login_id = ? COLLATE NOCASE
                """,
                (normalized_login_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_users(self) -> list:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, login_id, login_type, role, is_active, created_at, updated_at,
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

    def clear_all_users(self) -> int:
        """Delete every user and cascade all user-owned records."""
        with self.connect() as conn:
            before = int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])
            conn.execute("DELETE FROM users")
            conn.execute("DELETE FROM sqlite_sequence WHERE name = ?", ("users",))
        return before

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
                    u.login_id,
                    u.login_type,
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

    def save_questionnaire_definition(self, definition: Dict[str, Any]) -> None:
        """Persist one immutable questionnaire definition by version."""
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO questionnaire_definitions(
                    questionnaire_version, schema_version, definition_json, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    definition["questionnaire_version"],
                    definition["schema_version"],
                    _json_dumps(definition),
                    utc_now(),
                ),
            )

    def save_onboarding_bundle(
        self,
        user_id: int,
        response: Dict[str, Any],
        inference_run: Dict[str, Any],
        profile_snapshot: Dict[str, Any],
        routine_plan: Dict[str, Any],
        daily_context: Dict[str, Any],
    ) -> None:
        """Atomically save raw answers and every derived, versioned artifact."""
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO questionnaire_responses(
                    response_id, user_id, questionnaire_version, schema_version,
                    timezone, answers_json, submitted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    response["response_id"],
                    int(user_id),
                    response["questionnaire_version"],
                    response["schema_version"],
                    response["timezone"],
                    _json_dumps(response["answers"]),
                    response["submitted_at"],
                ),
            )
            conn.execute(
                """
                INSERT INTO profile_inference_runs(
                    profile_inference_run_id, user_id, response_id, mapping_version,
                    schema_version, result_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    inference_run["profile_inference_run_id"],
                    int(user_id),
                    response["response_id"],
                    inference_run["mapping_version"],
                    inference_run["schema_version"],
                    _json_dumps(inference_run),
                    inference_run["created_at"],
                ),
            )
            conn.execute(
                """
                INSERT INTO profile_snapshots(
                    profile_snapshot_id, user_id, profile_inference_run_id,
                    mapping_version, schema_version, snapshot_json, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    profile_snapshot["profile_snapshot_id"],
                    int(user_id),
                    inference_run["profile_inference_run_id"],
                    profile_snapshot["mapping_version"],
                    profile_snapshot["schema_version"],
                    _json_dumps(profile_snapshot),
                    profile_snapshot["created_at"],
                    profile_snapshot.get("expires_at"),
                ),
            )
            self._insert_routine_plan(conn, int(user_id), routine_plan)
            self._insert_daily_context(conn, int(user_id), daily_context)

    def latest_profile_snapshot(self, user_id: int) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT snapshot_json
                FROM profile_snapshots
                WHERE user_id = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (int(user_id),),
            ).fetchone()
        return self._load_json_column(row, "snapshot_json")

    def latest_questionnaire_response(self, user_id: int) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT response_id, questionnaire_version, schema_version,
                       timezone, answers_json, submitted_at
                FROM questionnaire_responses
                WHERE user_id = ?
                ORDER BY submitted_at DESC
                LIMIT 1
                """,
                (int(user_id),),
            ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["answers"] = json.loads(result.pop("answers_json"))
        return result

    def save_routine_plan(self, user_id: int, routine_plan: Dict[str, Any]) -> None:
        with self.connect() as conn:
            self._insert_routine_plan(conn, int(user_id), routine_plan)

    def get_routine_plan(self, user_id: int, local_date: str) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT plan_json
                FROM routine_plans
                WHERE user_id = ? AND local_date = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (int(user_id), local_date),
            ).fetchone()
        return self._load_json_column(row, "plan_json")

    def save_daily_context(self, user_id: int, context: Dict[str, Any]) -> None:
        with self.connect() as conn:
            self._insert_daily_context(conn, int(user_id), context)

    def update_daily_context_previous_day(
        self,
        user_id: int,
        context_snapshot_id: str,
        previous_day: Optional[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        """Attach the resolved cross-day provenance to an existing snapshot."""

        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT snapshot_json
                FROM daily_context_snapshots
                WHERE context_snapshot_id = ? AND user_id = ?
                """,
                (str(context_snapshot_id), int(user_id)),
            ).fetchone()
            if row is None:
                return None
            context = json.loads(row["snapshot_json"])
            context["schema_version"] = "daily_context.v2"
            context["previous_day"] = previous_day
            conn.execute(
                """
                UPDATE daily_context_snapshots
                SET schema_version = ?, snapshot_json = ?
                WHERE context_snapshot_id = ? AND user_id = ?
                """,
                (
                    context["schema_version"],
                    _json_dumps(context),
                    str(context_snapshot_id),
                    int(user_id),
                ),
            )
        return context

    def save_prediction_run(
        self,
        user_id: int,
        prediction: Dict[str, Any],
        state_points: list,
    ) -> None:
        """Persist a reproducible run and its complete raw state trajectory."""
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO prediction_runs(
                    prediction_run_id, user_id, context_snapshot_id, local_date,
                    schema_version, model_version, parameter_version,
                    feature_version, random_seed, input_json, result_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    prediction["prediction_run_id"],
                    int(user_id),
                    prediction.get("context_snapshot_id"),
                    prediction["local_date"],
                    prediction["schema_version"],
                    prediction["model_version"],
                    prediction["parameter_version"],
                    prediction["feature_version"],
                    int(prediction["random_seed"]),
                    _json_dumps(prediction["input"]),
                    _json_dumps(prediction["result"]),
                    prediction["created_at"],
                ),
            )
            conn.executemany(
                """
                INSERT INTO state_points(
                    prediction_run_id, point_index, local_time,
                    stress, energy, state, point_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        prediction["prediction_run_id"],
                        index,
                        point["time"],
                        float(point["S"]),
                        float(point["E"]),
                        point["state"],
                        _json_dumps(point),
                    )
                    for index, point in enumerate(state_points)
                ],
            )
            diagnostics = prediction.get("diagnostics")
            if isinstance(diagnostics, dict):
                conn.execute(
                    """
                    INSERT INTO prediction_diagnostics(
                        prediction_run_id, diagnostics_json, created_at
                    ) VALUES (?, ?, ?)
                    """,
                    (
                        prediction["prediction_run_id"],
                        _json_dumps(diagnostics),
                        prediction["created_at"],
                    ),
                )

    def recent_prediction_runs(self, user_id: int, limit: int = 8) -> list:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT prediction_run_id, local_date, model_version,
                       parameter_version, random_seed, result_json, created_at
                FROM prediction_runs
                WHERE user_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (int(user_id), max(1, min(int(limit), 50))),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["result"] = json.loads(item.pop("result_json"))
            result.append(item)
        return result

    def latest_prediction_run_for_date(
        self,
        user_id: int,
        local_date: str,
    ) -> Optional[Dict[str, Any]]:
        """Load the newest complete run for one exact user-local date."""

        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT prediction_run_id
                FROM prediction_runs
                WHERE user_id = ? AND local_date = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (int(user_id), str(local_date)),
            ).fetchone()
        if row is None:
            return None
        return self.prediction_run_detail_for_user(
            int(user_id),
            str(row["prediction_run_id"]),
        )

    def prediction_run_detail_for_user(
        self,
        user_id: int,
        prediction_run_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Load a frozen run only when it belongs to the requesting user."""

        with self.connect() as conn:
            run = conn.execute(
                """
                SELECT *
                FROM prediction_runs
                WHERE prediction_run_id = ? AND user_id = ?
                """,
                (str(prediction_run_id), int(user_id)),
            ).fetchone()
            if run is None:
                return None
            point_rows = conn.execute(
                """
                SELECT point_json
                FROM state_points
                WHERE prediction_run_id = ?
                ORDER BY point_index
                """,
                (str(prediction_run_id),),
            ).fetchall()
            diagnostic_row = conn.execute(
                """
                SELECT diagnostics_json
                FROM prediction_diagnostics
                WHERE prediction_run_id = ?
                """,
                (str(prediction_run_id),),
            ).fetchone()
            feedback_rows = conn.execute(
                """
                SELECT feedback_id, feedback_type, target_time,
                       payload_json, reported_at, retrospective
                FROM feedback_observations
                WHERE prediction_run_id = ?
                ORDER BY reported_at DESC
                """,
                (str(prediction_run_id),),
            ).fetchall()

        item = dict(run)
        item["input"] = json.loads(item.pop("input_json"))
        item["result"] = json.loads(item.pop("result_json"))
        item["points"] = [json.loads(row["point_json"]) for row in point_rows]
        item["diagnostics"] = (
            json.loads(diagnostic_row["diagnostics_json"])
            if diagnostic_row
            else None
        )
        item["feedback"] = []
        for row in feedback_rows:
            feedback = dict(row)
            feedback["payload"] = json.loads(feedback.pop("payload_json"))
            feedback["retrospective"] = bool(feedback["retrospective"])
            item["feedback"].append(feedback)
        return item

    def user_owns_prediction_run(self, user_id: int, prediction_run_id: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM prediction_runs
                WHERE prediction_run_id = ? AND user_id = ?
                """,
                (str(prediction_run_id), int(user_id)),
            ).fetchone()
        return row is not None

    def admin_overview(self) -> Dict[str, Any]:
        """Return management aggregates backed by the application database."""
        self.init_schema()
        with self.connect() as conn:
            counts = {
                "users": int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]),
                "active_users": int(
                    conn.execute(
                        "SELECT COUNT(*) FROM users WHERE is_active = 1"
                    ).fetchone()[0]
                ),
                "admins": int(
                    conn.execute(
                        "SELECT COUNT(*) FROM users WHERE role = 'admin'"
                    ).fetchone()[0]
                ),
                "profiles": int(
                    conn.execute(
                        "SELECT COUNT(DISTINCT user_id) FROM profile_snapshots"
                    ).fetchone()[0]
                ),
                "prediction_runs": int(
                    conn.execute("SELECT COUNT(*) FROM prediction_runs").fetchone()[0]
                ),
                "feedback": int(
                    conn.execute(
                        "SELECT COUNT(*) FROM feedback_observations"
                    ).fetchone()[0]
                ),
                "diagnostic_runs": int(
                    conn.execute(
                        "SELECT COUNT(*) FROM prediction_diagnostics"
                    ).fetchone()[0]
                ),
            }
            recent_activity = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT local_date AS date, COUNT(*) AS runs
                    FROM prediction_runs
                    WHERE local_date >= date('now', '-13 days')
                    GROUP BY local_date
                    ORDER BY local_date
                    """
                ).fetchall()
            ]
            feedback_types = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT feedback_type, COUNT(*) AS count
                    FROM feedback_observations
                    GROUP BY feedback_type
                    ORDER BY count DESC, feedback_type
                    """
                ).fetchall()
            ]
            users = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT u.id, u.login_id, u.login_type, u.role, u.is_active,
                           u.created_at, u.last_login_at,
                           (
                               SELECT COUNT(*)
                               FROM prediction_runs pr
                               WHERE pr.user_id = u.id
                           ) AS run_count,
                           (
                               SELECT COUNT(*)
                               FROM feedback_observations fo
                               WHERE fo.user_id = u.id
                           ) AS feedback_count,
                           EXISTS(
                               SELECT 1
                               FROM profile_snapshots ps
                               WHERE ps.user_id = u.id
                           ) AS has_profile
                    FROM users u
                    ORDER BY u.created_at DESC
                    LIMIT 100
                    """
                ).fetchall()
            ]
            profile_rows = conn.execute(
                "SELECT params_json FROM user_profiles"
            ).fetchall()

        strategy_distribution: Dict[str, Dict[str, int]] = {
            "f_strategy": {},
            "C_strategy": {},
            "night_strategy": {},
            "rest_strategy": {},
        }
        for row in profile_rows:
            try:
                params = json.loads(row["params_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(params, dict):
                continue
            for family in strategy_distribution:
                selected = params.get(family)
                if selected:
                    key = str(selected)
                    strategy_distribution[family][key] = (
                        strategy_distribution[family].get(key, 0) + 1
                    )

        for item in users:
            item["is_active"] = bool(item["is_active"])
            item["has_profile"] = bool(item["has_profile"])

        return {
            "counts": counts,
            "profile_coverage": (
                counts["profiles"] / counts["users"] if counts["users"] else 0.0
            ),
            "feedback_per_run": (
                counts["feedback"] / counts["prediction_runs"]
                if counts["prediction_runs"]
                else 0.0
            ),
            "recent_activity": recent_activity,
            "feedback_types": feedback_types,
            "strategy_distribution": strategy_distribution,
            "users": users,
        }

    def admin_prediction_runs(
        self,
        limit: int = 30,
        user_id: Optional[int] = None,
    ) -> list:
        """List replayable runs without loading every stored state point."""
        safe_limit = max(1, min(int(limit), 100))
        where = ""
        values: list[Any] = []
        if user_id is not None:
            where = "WHERE pr.user_id = ?"
            values.append(int(user_id))
        values.append(safe_limit)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT pr.prediction_run_id, pr.user_id, u.login_id,
                       pr.local_date, pr.model_version, pr.parameter_version,
                       pr.feature_version, pr.random_seed, pr.input_json,
                       pr.result_json, pr.created_at,
                       COUNT(DISTINCT sp.point_index) AS point_count,
                       COUNT(DISTINCT fo.feedback_id) AS feedback_count,
                       CASE WHEN pd.prediction_run_id IS NULL THEN 0 ELSE 1 END
                           AS has_diagnostics
                FROM prediction_runs pr
                JOIN users u ON u.id = pr.user_id
                LEFT JOIN state_points sp
                    ON sp.prediction_run_id = pr.prediction_run_id
                LEFT JOIN feedback_observations fo
                    ON fo.prediction_run_id = pr.prediction_run_id
                LEFT JOIN prediction_diagnostics pd
                    ON pd.prediction_run_id = pr.prediction_run_id
                {where}
                GROUP BY pr.prediction_run_id
                ORDER BY pr.created_at DESC
                LIMIT ?
                """,
                values,
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["input"] = json.loads(item.pop("input_json"))
            item["result"] = json.loads(item.pop("result_json"))
            item["has_diagnostics"] = bool(item["has_diagnostics"])
            result.append(item)
        return result

    def admin_prediction_run_detail(
        self,
        prediction_run_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Load one run, its step-level components, feedback, and formula trace."""
        with self.connect() as conn:
            run = conn.execute(
                """
                SELECT pr.*, u.login_id
                FROM prediction_runs pr
                JOIN users u ON u.id = pr.user_id
                WHERE pr.prediction_run_id = ?
                """,
                (str(prediction_run_id),),
            ).fetchone()
            if run is None:
                return None
            point_rows = conn.execute(
                """
                SELECT point_json
                FROM state_points
                WHERE prediction_run_id = ?
                ORDER BY point_index
                """,
                (str(prediction_run_id),),
            ).fetchall()
            diagnostic_row = conn.execute(
                """
                SELECT diagnostics_json
                FROM prediction_diagnostics
                WHERE prediction_run_id = ?
                """,
                (str(prediction_run_id),),
            ).fetchone()
            feedback_rows = conn.execute(
                """
                SELECT feedback_id, feedback_type, target_time,
                       payload_json, reported_at, retrospective
                FROM feedback_observations
                WHERE prediction_run_id = ?
                ORDER BY reported_at DESC
                """,
                (str(prediction_run_id),),
            ).fetchall()

        item = dict(run)
        item["input"] = json.loads(item.pop("input_json"))
        item["result"] = json.loads(item.pop("result_json"))
        item["points"] = [json.loads(row["point_json"]) for row in point_rows]
        item["diagnostics"] = (
            json.loads(diagnostic_row["diagnostics_json"])
            if diagnostic_row
            else None
        )
        item["feedback"] = []
        for row in feedback_rows:
            feedback = dict(row)
            feedback["payload"] = json.loads(feedback.pop("payload_json"))
            feedback["retrospective"] = bool(feedback["retrospective"])
            item["feedback"].append(feedback)
        return item

    def recent_audit_logs(self, limit: int = 40) -> list:
        """Return recent administrative and account actions."""
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT al.id, al.action, al.user_id, u.login_id,
                       al.ip_address, al.details_json, al.created_at
                FROM audit_logs al
                LEFT JOIN users u ON u.id = al.user_id
                ORDER BY al.created_at DESC
                LIMIT ?
                """,
                (max(1, min(int(limit), 200)),),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["details"] = json.loads(item.pop("details_json") or "{}")
            except json.JSONDecodeError:
                item["details"] = {}
            result.append(item)
        return result

    def save_feedback_observation(
        self,
        user_id: int,
        feedback: Dict[str, Any],
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO feedback_observations(
                    feedback_id, user_id, prediction_run_id, feedback_type,
                    schema_version, target_time, payload_json, reported_at, retrospective
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    feedback["feedback_id"],
                    int(user_id),
                    feedback.get("prediction_run_id"),
                    feedback["feedback_type"],
                    feedback["schema_version"],
                    feedback.get("target_time"),
                    _json_dumps(feedback["payload"]),
                    feedback["reported_at"],
                    int(bool(feedback.get("retrospective"))),
                ),
            )

    def list_feedback_observations(
        self,
        user_id: int,
        *,
        target_date: Optional[str] = None,
        limit: int = 200,
    ) -> list[Dict[str, Any]]:
        """Return raw EMA/review observations owned by one user."""

        clauses = ["user_id = ?"]
        values: list[Any] = [int(user_id)]
        if target_date:
            clauses.append(
                "(substr(target_time, 1, 10) = ? OR substr(reported_at, 1, 10) = ?)"
            )
            values.extend([str(target_date), str(target_date)])
        values.append(max(1, min(int(limit), 1000)))
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT feedback_id, prediction_run_id, feedback_type,
                       schema_version, target_time, payload_json, reported_at,
                       retrospective
                FROM feedback_observations
                WHERE {' AND '.join(clauses)}
                ORDER BY COALESCE(target_time, reported_at) ASC
                LIMIT ?
                """,
                values,
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["payload"] = json.loads(item.pop("payload_json") or "{}")
            except json.JSONDecodeError:
                item["payload"] = {}
            item["retrospective"] = bool(item.get("retrospective"))
            result.append(item)
        return result

    def store_bot_binding_token(
        self,
        *,
        token_id: str,
        token_hash: str,
        app_id: str,
        tenant_key: str,
        open_id: str,
        chat_id: Optional[str],
        expires_at: str,
    ) -> None:
        """Persist only a one-way hash of a short-lived binding credential."""

        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE bot_binding_tokens
                SET used_at = ?
                WHERE app_id = ? AND open_id = ? AND used_at IS NULL
                """,
                (now, str(app_id), str(open_id)),
            )
            conn.execute(
                """
                INSERT INTO bot_binding_tokens(
                    token_id, token_hash, app_id, tenant_key, open_id, chat_id,
                    expires_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(token_id),
                    str(token_hash),
                    str(app_id),
                    str(tenant_key or ""),
                    str(open_id),
                    str(chat_id) if chat_id else None,
                    str(expires_at),
                    now,
                ),
            )

    def confirm_feishu_binding(
        self,
        *,
        token_hash: str,
        user_id: int,
    ) -> Dict[str, Any]:
        """Consume one token and atomically create its one-to-one binding."""

        now = utc_now()
        user_id = int(user_id)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            user = conn.execute(
                "SELECT id, is_active FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
            if not user or not user["is_active"]:
                raise ValueError("当前项目账号不可用")
            token = conn.execute(
                """
                SELECT * FROM bot_binding_tokens
                WHERE token_hash = ? AND used_at IS NULL AND expires_at > ?
                """,
                (str(token_hash), now),
            ).fetchone()
            if token is None:
                raise ValueError("绑定链接无效、已使用或已过期")

            open_binding = conn.execute(
                """
                SELECT id, user_id FROM feishu_user_bindings
                WHERE app_id = ? AND open_id = ? AND binding_status = 'active'
                """,
                (token["app_id"], token["open_id"]),
            ).fetchone()
            if open_binding and int(open_binding["user_id"]) != user_id:
                raise ValueError("该飞书账号已绑定其他项目账号")

            user_binding = conn.execute(
                """
                SELECT id, open_id FROM feishu_user_bindings
                WHERE app_id = ? AND user_id = ? AND binding_status = 'active'
                """,
                (token["app_id"], user_id),
            ).fetchone()
            if user_binding and user_binding["open_id"] != token["open_id"]:
                raise ValueError("当前项目账号已绑定其他飞书账号，请先解绑")

            if open_binding:
                binding_id = int(open_binding["id"])
                conn.execute(
                    """
                    UPDATE feishu_user_bindings
                    SET tenant_key = ?, chat_id = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (token["tenant_key"], token["chat_id"], now, binding_id),
                )
            else:
                cursor = conn.execute(
                    """
                    INSERT INTO feishu_user_bindings(
                        user_id, app_id, tenant_key, open_id, chat_id,
                        binding_status, bound_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?)
                    """,
                    (
                        user_id,
                        token["app_id"],
                        token["tenant_key"],
                        token["open_id"],
                        token["chat_id"],
                        now,
                        now,
                        now,
                    ),
                )
                binding_id = int(cursor.lastrowid)

            consumed = conn.execute(
                """
                UPDATE bot_binding_tokens
                SET used_at = ?
                WHERE token_id = ? AND used_at IS NULL
                """,
                (now, token["token_id"]),
            )
            if consumed.rowcount != 1:
                raise ValueError("绑定链接已被使用")

            binding = conn.execute(
                """
                SELECT id, user_id, app_id, tenant_key, open_id, chat_id,
                       binding_status, bound_at, revoked_at, created_at, updated_at
                FROM feishu_user_bindings WHERE id = ?
                """,
                (binding_id,),
            ).fetchone()
        return dict(binding)

    def resolve_feishu_binding(
        self,
        *,
        app_id: str,
        tenant_key: str,
        open_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Resolve a bot identity and include the project account active flag."""

        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT b.id, b.user_id, b.app_id, b.tenant_key, b.open_id,
                       b.chat_id, b.binding_status, b.bound_at, b.updated_at,
                       u.is_active AS user_is_active
                FROM feishu_user_bindings AS b
                JOIN users AS u ON u.id = b.user_id
                WHERE b.app_id = ? AND b.open_id = ?
                  AND b.binding_status = 'active'
                  AND (b.tenant_key = ? OR b.tenant_key = '' OR ? = '')
                ORDER BY CASE WHEN b.tenant_key = ? THEN 0 ELSE 1 END
                LIMIT 1
                """,
                (
                    str(app_id),
                    str(open_id),
                    str(tenant_key or ""),
                    str(tenant_key or ""),
                    str(tenant_key or ""),
                ),
            ).fetchone()
        if not row:
            return None
        item = dict(row)
        item["user_is_active"] = bool(item["user_is_active"])
        return item

    def feishu_binding_for_user(
        self,
        user_id: int,
        *,
        app_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        clauses = ["user_id = ?", "binding_status = 'active'"]
        values: list[Any] = [int(user_id)]
        if app_id:
            clauses.append("app_id = ?")
            values.append(str(app_id))
        with self.connect() as conn:
            row = conn.execute(
                f"""
                SELECT id, user_id, app_id, tenant_key, open_id, chat_id,
                       binding_status, bound_at, updated_at
                FROM feishu_user_bindings
                WHERE {' AND '.join(clauses)}
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                values,
            ).fetchone()
        return dict(row) if row else None

    def revoke_feishu_binding(
        self,
        user_id: int,
        *,
        app_id: Optional[str] = None,
    ) -> bool:
        clauses = ["user_id = ?", "binding_status = 'active'"]
        values: list[Any] = [int(user_id)]
        if app_id:
            clauses.append("app_id = ?")
            values.append(str(app_id))
        now = utc_now()
        with self.connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE feishu_user_bindings
                SET binding_status = 'revoked', revoked_at = ?, updated_at = ?
                WHERE {' AND '.join(clauses)}
                """,
                [now, now, *values],
            )
        return cursor.rowcount > 0

    def enqueue_feishu_event(self, event: Dict[str, Any]) -> bool:
        """Persist the minimal normalized event and deduplicate provider retries."""

        now = utc_now()
        try:
            with self.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO feishu_inbox_events(
                        event_id, message_id, app_id, tenant_key, sender_open_id,
                        chat_id, chat_type, message_type, event_type, content_json,
                        status, attempts, available_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'received', 0, ?, ?, ?)
                    """,
                    (
                        str(event["event_id"]),
                        str(event.get("message_id") or "") or None,
                        str(event["app_id"]),
                        str(event.get("tenant_key") or ""),
                        str(event["sender_open_id"]),
                        str(event["chat_id"]),
                        str(event.get("chat_type") or "p2p"),
                        str(event.get("message_type") or "unknown"),
                        str(event.get("event_type") or "message"),
                        _json_dumps(event.get("content") or {}),
                        now,
                        now,
                        now,
                    ),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def claim_feishu_event(
        self,
        worker_id: str,
        *,
        lease_seconds: int = 120,
        max_attempts: int = 5,
    ) -> Optional[Dict[str, Any]]:
        """Atomically claim one due event and recover abandoned processing leases."""

        now = utc_now()
        lease_cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=max(1, int(lease_seconds)))
        ).isoformat(timespec="seconds")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                UPDATE feishu_inbox_events
                SET status = 'retry_wait', available_at = ?, claimed_at = NULL,
                    claimed_by = NULL, updated_at = ?,
                    last_error = 'processing lease expired'
                WHERE status = 'processing' AND claimed_at < ?
                """,
                (now, now, lease_cutoff),
            )
            row = conn.execute(
                """
                SELECT event_id
                FROM feishu_inbox_events
                WHERE status IN ('received', 'retry_wait')
                  AND available_at <= ? AND attempts < ?
                ORDER BY available_at, created_at
                LIMIT 1
                """,
                (now, max(1, int(max_attempts))),
            ).fetchone()
            if row is None:
                return None
            cursor = conn.execute(
                """
                UPDATE feishu_inbox_events
                SET status = 'processing', attempts = attempts + 1,
                    claimed_at = ?, claimed_by = ?, updated_at = ?
                WHERE event_id = ? AND status IN ('received', 'retry_wait')
                """,
                (now, str(worker_id), now, row["event_id"]),
            )
            if cursor.rowcount != 1:
                return None
            claimed = conn.execute(
                "SELECT * FROM feishu_inbox_events WHERE event_id = ?",
                (row["event_id"],),
            ).fetchone()
        item = dict(claimed)
        try:
            item["content"] = json.loads(item.pop("content_json") or "{}")
        except json.JSONDecodeError:
            item["content"] = {}
        return item

    def complete_feishu_event(self, event_id: str, *, ignored: bool = False) -> bool:
        now = utc_now()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE feishu_inbox_events
                SET status = ?, completed_at = ?, updated_at = ?,
                    claimed_at = NULL, claimed_by = NULL, last_error = NULL,
                    content_json = '{}'
                WHERE event_id = ? AND status = 'processing'
                """,
                ("ignored" if ignored else "completed", now, now, str(event_id)),
            )
        return cursor.rowcount == 1

    def retry_feishu_event(
        self,
        event_id: str,
        error: str,
        *,
        max_attempts: int = 5,
        delay_seconds: int = 1,
        retryable: bool = True,
    ) -> str:
        """Move a claimed event to retry/dead-letter without retaining sensitive errors."""

        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat(timespec="seconds")
        available_at = (now_dt + timedelta(seconds=max(0, int(delay_seconds)))).isoformat(
            timespec="seconds"
        )
        safe_error = re.sub(r"(access|refresh|tenant)[-_ ]?token\s*[:=]\s*\S+", r"\1_token=[redacted]", str(error), flags=re.I)[:500]
        with self.connect() as conn:
            row = conn.execute(
                "SELECT attempts FROM feishu_inbox_events WHERE event_id = ?",
                (str(event_id),),
            ).fetchone()
            if row is None:
                raise ValueError("event not found")
            exhausted = int(row["attempts"]) >= max(1, int(max_attempts))
            status = "dead_letter" if exhausted else ("retry_wait" if retryable else "failed")
            conn.execute(
                """
                UPDATE feishu_inbox_events
                SET status = ?, available_at = ?, claimed_at = NULL,
                    claimed_by = NULL, last_error = ?, updated_at = ?
                WHERE event_id = ?
                """,
                (status, available_at, safe_error, now, str(event_id)),
            )
        return status

    def retry_failed_feishu_event(self, event_id: str) -> bool:
        now = utc_now()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE feishu_inbox_events
                SET status = 'retry_wait', attempts = 0, available_at = ?,
                    claimed_at = NULL, claimed_by = NULL, completed_at = NULL,
                    last_error = NULL, updated_at = ?
                WHERE event_id = ? AND status IN ('failed', 'dead_letter')
                """,
                (now, now, str(event_id)),
            )
        return cursor.rowcount == 1

    def feishu_bot_failures(self, limit: int = 50) -> list[Dict[str, Any]]:
        """Return failure metadata only; never expose sender IDs or message content."""

        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT event_id, event_type, message_type, status, attempts,
                       available_at, last_error, created_at, updated_at
                FROM feishu_inbox_events
                WHERE status IN ('failed', 'dead_letter')
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (max(1, min(int(limit), 200)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def update_bot_heartbeat(
        self,
        process_name: str,
        status: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO bot_runtime_heartbeats(
                    process_name, status, details_json, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(process_name) DO UPDATE SET
                    status = excluded.status,
                    details_json = excluded.details_json,
                    updated_at = excluded.updated_at
                """,
                (str(process_name), str(status), _json_dumps(details or {}), utc_now()),
            )

    def feishu_bot_status(self) -> Dict[str, Any]:
        with self.connect() as conn:
            queue_rows = conn.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM feishu_inbox_events GROUP BY status
                """
            ).fetchall()
            delivery_rows = conn.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM care_deliveries
                WHERE local_date = date('now')
                GROUP BY status
                """
            ).fetchall()
            heartbeat_rows = conn.execute(
                """
                SELECT process_name, status, details_json, updated_at
                FROM bot_runtime_heartbeats ORDER BY process_name
                """
            ).fetchall()
            active_bindings = int(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM feishu_user_bindings
                    WHERE binding_status = 'active'
                    """
                ).fetchone()[0]
            )
        heartbeats = []
        for row in heartbeat_rows:
            item = dict(row)
            try:
                item["details"] = json.loads(item.pop("details_json") or "{}")
            except json.JSONDecodeError:
                item["details"] = {}
            heartbeats.append(item)
        return {
            "queue": {row["status"]: int(row["count"]) for row in queue_rows},
            "today_deliveries": {
                row["status"]: int(row["count"]) for row in delivery_rows
            },
            "heartbeats": heartbeats,
            "active_bindings": active_bindings,
        }

    def get_care_preferences(
        self,
        user_id: int,
        *,
        defaults: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        defaults = dict(defaults or {})
        quiet_hours = defaults.get("quiet_hours") or ["23:00", "07:00"]
        if not isinstance(quiet_hours, (list, tuple)) or len(quiet_hours) != 2:
            quiet_hours = ["23:00", "07:00"]
        support = defaults.get("preferred_support") or []
        if isinstance(support, str):
            support = [support]
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO care_channel_preferences(
                    user_id, feishu_proactive_enabled, quiet_start, quiet_end,
                    max_daily_messages, tone, preferred_support_json,
                    allow_personal_history_reference, allow_external_llm, updated_at
                ) VALUES (?, 0, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(user_id),
                    str(quiet_hours[0]),
                    str(quiet_hours[1]),
                    int(defaults.get("max_daily_messages", 2)),
                    str(defaults.get("tone") or "supportive"),
                    _json_dumps(list(support)),
                    int(bool(defaults.get("allow_personal_history_reference", False))),
                    int(bool(defaults.get("allow_external_llm", False))),
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM care_channel_preferences WHERE user_id = ?",
                (int(user_id),),
            ).fetchone()
        item = dict(row)
        item["feishu_proactive_enabled"] = bool(item["feishu_proactive_enabled"])
        item["allow_personal_history_reference"] = bool(
            item["allow_personal_history_reference"]
        )
        item["allow_external_llm"] = bool(item["allow_external_llm"])
        try:
            item["preferred_support"] = json.loads(
                item.pop("preferred_support_json") or "[]"
            )
        except json.JSONDecodeError:
            item["preferred_support"] = []
        return item

    def update_care_preferences(
        self,
        user_id: int,
        changes: Dict[str, Any],
    ) -> Dict[str, Any]:
        current = self.get_care_preferences(int(user_id))
        merged = {**current, **dict(changes)}
        support = merged.get("preferred_support") or []
        if isinstance(support, str):
            support = [support]
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE care_channel_preferences
                SET feishu_proactive_enabled = ?, quiet_start = ?, quiet_end = ?,
                    max_daily_messages = ?, tone = ?, preferred_support_json = ?,
                    allow_personal_history_reference = ?, allow_external_llm = ?,
                    updated_at = ?
                WHERE user_id = ?
                """,
                (
                    int(bool(merged["feishu_proactive_enabled"])),
                    str(merged["quiet_start"]),
                    str(merged["quiet_end"]),
                    int(merged["max_daily_messages"]),
                    str(merged["tone"]),
                    _json_dumps(list(support)),
                    int(bool(merged["allow_personal_history_reference"])),
                    int(bool(merged["allow_external_llm"])),
                    utc_now(),
                    int(user_id),
                ),
            )
        return self.get_care_preferences(int(user_id))

    def create_care_delivery(
        self,
        *,
        delivery_id: str,
        user_id: int,
        local_date: str,
        episode_key: str,
        prediction_run_id: Optional[str] = None,
        tier: str = "support",
        channel: str = "feishu",
        status: str = "candidate",
        scheduled_at: Optional[str] = None,
    ) -> Dict[str, Any]:
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO care_deliveries(
                    delivery_id, user_id, prediction_run_id, local_date, tier,
                    episode_key, channel, status, scheduled_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(delivery_id),
                    int(user_id),
                    prediction_run_id,
                    str(local_date),
                    str(tier),
                    str(episode_key),
                    str(channel),
                    str(status),
                    scheduled_at,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                """
                SELECT * FROM care_deliveries
                WHERE user_id = ? AND local_date = ? AND episode_key = ? AND channel = ?
                """,
                (int(user_id), str(local_date), str(episode_key), str(channel)),
            ).fetchone()
        if row is None:
            raise RuntimeError("care delivery could not be created")
        return dict(row)

    def care_delivery_for_user(
        self,
        user_id: int,
        delivery_id: str,
    ) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM care_deliveries
                WHERE delivery_id = ? AND user_id = ?
                """,
                (str(delivery_id), int(user_id)),
            ).fetchone()
        return dict(row) if row else None

    def mark_care_delivery_sent(
        self,
        delivery_id: str,
        provider_message_id: str,
    ) -> bool:
        now = utc_now()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE care_deliveries
                SET status = 'sent', sent_at = ?, provider_message_id = ?,
                    failure_reason = NULL, updated_at = ?
                WHERE delivery_id = ?
                """,
                (now, str(provider_message_id), now, str(delivery_id)),
            )
        return cursor.rowcount == 1

    def mark_care_delivery_failed(self, delivery_id: str, reason: str) -> bool:
        safe_reason = str(reason)[:500]
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE care_deliveries
                SET status = 'failed', failure_reason = ?, attempts = attempts + 1,
                    updated_at = ?
                WHERE delivery_id = ?
                """,
                (safe_reason, utc_now(), str(delivery_id)),
            )
        return cursor.rowcount == 1

    @staticmethod
    def _insert_routine_plan(
        conn: sqlite3.Connection,
        user_id: int,
        routine_plan: Dict[str, Any],
    ) -> None:
        conn.execute(
            """
            INSERT INTO routine_plans(
                routine_plan_id, user_id, local_date, profile_snapshot_id,
                schema_version, rule_version, plan_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                routine_plan["routine_plan_id"],
                user_id,
                routine_plan["local_date"],
                routine_plan["profile_snapshot_id"],
                routine_plan["schema_version"],
                routine_plan["rule_version"],
                _json_dumps(routine_plan),
                routine_plan["created_at"],
            ),
        )

    @staticmethod
    def _insert_daily_context(
        conn: sqlite3.Connection,
        user_id: int,
        context: Dict[str, Any],
    ) -> None:
        conn.execute(
            """
            INSERT INTO daily_context_snapshots(
                context_snapshot_id, user_id, target_date, profile_snapshot_id,
                routine_plan_id, schema_version, snapshot_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                context["context_snapshot_id"],
                user_id,
                context["target_date"],
                context["profile_snapshot_id"],
                context["routine_plan_id"],
                context["schema_version"],
                _json_dumps(context),
                context["created_at"],
            ),
        )

    @staticmethod
    def _load_json_column(
        row: Optional[sqlite3.Row],
        column: str,
    ) -> Optional[Dict[str, Any]]:
        if not row:
            return None
        try:
            value = json.loads(row[column])
            return value if isinstance(value, dict) else None
        except (TypeError, json.JSONDecodeError):
            return None

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
            for table in (
                "users",
                "api_keys",
                "user_profiles",
                "audit_logs",
                "questionnaire_responses",
                "profile_snapshots",
                "routine_plans",
                "prediction_runs",
                "feedback_observations",
                "feishu_user_bindings",
                "bot_binding_tokens",
                "feishu_inbox_events",
                "care_deliveries",
                "care_channel_preferences",
                "bot_runtime_heartbeats",
            ):
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
    def _validate_login_id(login_id: str) -> tuple[str, str]:
        if not isinstance(login_id, str):
            raise ValueError("请输入邮箱或学号")
        value = login_id.strip()
        if "@" in value:
            normalized = value.lower()
            if len(normalized) > 254 or not EMAIL_PATTERN.fullmatch(normalized):
                raise ValueError("请输入有效的邮箱地址")
            return normalized, "email"
        if not STUDENT_ID_PATTERN.fullmatch(value) or not any(
            character.isdigit() for character in value
        ):
            raise ValueError("学号需为 5–32 位字母、数字、下划线或连字符，并至少包含一个数字")
        return value, "student_id"

    @staticmethod
    def _validate_password(password: str) -> None:
        if not isinstance(password, str):
            raise ValueError("password must be a string")
        if len(password) < 10:
            raise ValueError("password must contain at least 10 characters")
