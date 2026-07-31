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
