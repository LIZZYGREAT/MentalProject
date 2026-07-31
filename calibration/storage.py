"""SQLite storage for feedback, model runs, evaluations, and calibration jobs."""

from __future__ import annotations

from datetime import datetime
import json
import os
import sqlite3
from typing import Any, Dict, Iterable, Optional

from settings.model_defaults import BASE_DATA_DIR, DEFAULT_USER_ID


DEFAULT_DB_PATH = os.path.join(BASE_DATA_DIR, "calibration", "calibration.sqlite3")


def _utc_now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def _json_dumps(value: Any) -> str:
    return json.dumps(_json_safe(value), ensure_ascii=False, sort_keys=True)


class CalibrationStore:
    """Small SQLite gateway for the local calibration loop."""

    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self.db_path = db_path

    def _connect(self) -> sqlite3.Connection:
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS daily_feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    date TEXT NOT NULL,
                    stress_morning REAL,
                    energy_morning REAL,
                    stress_noon REAL,
                    energy_noon REAL,
                    stress_evening REAL,
                    energy_evening REAL,
                    stress_peak_time TEXT,
                    energy_low_time TEXT,
                    expected_alert_level INTEGER,
                    sleep_quality REAL,
                    sleep_hours REAL,
                    dominant_event TEXT,
                    notes TEXT,
                    raw_json TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_daily_feedback_user_date
                    ON daily_feedback(user_id, date);

                CREATE TABLE IF NOT EXISTS event_feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    date TEXT NOT NULL,
                    event_id TEXT,
                    event_name TEXT,
                    perceived_stress REAL,
                    perceived_energy_cost REAL,
                    classification_correct INTEGER,
                    corrected_type TEXT,
                    corrected_task_type TEXT,
                    notes TEXT,
                    raw_json TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_event_feedback_user_date
                    ON event_feedback(user_id, date);

                CREATE TABLE IF NOT EXISTS parameter_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    version_name TEXT NOT NULL,
                    params_json TEXT NOT NULL,
                    parent_version TEXT,
                    notes TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS model_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    date TEXT NOT NULL,
                    model_version TEXT,
                    params_version TEXT,
                    input_json TEXT,
                    s_end REAL,
                    e_end REAL,
                    s_star REAL,
                    s_threshold REAL,
                    alerts_count INTEGER,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS curve_points (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL,
                    time TEXT NOT NULL,
                    stress REAL NOT NULL,
                    energy REAL NOT NULL,
                    state TEXT,
                    FOREIGN KEY(run_id) REFERENCES model_runs(id)
                );

                CREATE INDEX IF NOT EXISTS idx_curve_points_run_time
                    ON curve_points(run_id, time);

                CREATE TABLE IF NOT EXISTS evaluation_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    params_version TEXT,
                    sample_count INTEGER NOT NULL,
                    stress_mae REAL,
                    energy_mae REAL,
                    trend_accuracy REAL,
                    peak_time_error_min REAL,
                    alert_score REAL,
                    total_loss REAL,
                    metrics_json TEXT,
                    notes TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS calibration_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    base_params_version TEXT,
                    best_params_version TEXT,
                    status TEXT NOT NULL,
                    best_loss REAL,
                    search_space_json TEXT,
                    report_json TEXT,
                    started_at TEXT NOT NULL,
                    ended_at TEXT
                );
                """
            )

    def record_daily_feedback(self, data: Dict[str, Any], user_id: str = DEFAULT_USER_ID) -> int:
        self.init_schema()
        payload = dict(data)
        row = {
            "user_id": payload.get("user_id", user_id),
            "date": payload["date"],
            "stress_morning": payload.get("stress_morning"),
            "energy_morning": payload.get("energy_morning"),
            "stress_noon": payload.get("stress_noon"),
            "energy_noon": payload.get("energy_noon"),
            "stress_evening": payload.get("stress_evening"),
            "energy_evening": payload.get("energy_evening"),
            "stress_peak_time": payload.get("stress_peak_time"),
            "energy_low_time": payload.get("energy_low_time"),
            "expected_alert_level": payload.get("expected_alert_level"),
            "sleep_quality": payload.get("sleep_quality"),
            "sleep_hours": payload.get("sleep_hours"),
            "dominant_event": payload.get("dominant_event"),
            "notes": payload.get("notes"),
            "raw_json": _json_dumps(payload),
            "created_at": _utc_now(),
        }
        return self._insert("daily_feedback", row)

    def record_event_feedback(self, data: Dict[str, Any], user_id: str = DEFAULT_USER_ID) -> int:
        self.init_schema()
        payload = dict(data)
        row = {
            "user_id": payload.get("user_id", user_id),
            "date": payload["date"],
            "event_id": payload.get("event_id"),
            "event_name": payload.get("event_name"),
            "perceived_stress": payload.get("perceived_stress"),
            "perceived_energy_cost": payload.get("perceived_energy_cost"),
            "classification_correct": payload.get("classification_correct"),
            "corrected_type": payload.get("corrected_type"),
            "corrected_task_type": payload.get("corrected_task_type"),
            "notes": payload.get("notes"),
            "raw_json": _json_dumps(payload),
            "created_at": _utc_now(),
        }
        return self._insert("event_feedback", row)

    def record_parameter_version(
        self,
        params: Dict[str, Any],
        version_name: str,
        user_id: str = DEFAULT_USER_ID,
        parent_version: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> int:
        self.init_schema()
        return self._insert(
            "parameter_versions",
            {
                "user_id": user_id,
                "version_name": version_name,
                "params_json": _json_dumps(params),
                "parent_version": parent_version,
                "notes": notes,
                "created_at": _utc_now(),
            },
        )

    def record_model_run(
        self,
        date: str,
        results: Iterable[Dict[str, Any]],
        final_state: Dict[str, Any],
        alerts: Iterable[Dict[str, Any]],
        user_id: str = DEFAULT_USER_ID,
        params_version: Optional[str] = None,
        model_version: Optional[str] = None,
        input_payload: Optional[Dict[str, Any]] = None,
    ) -> int:
        self.init_schema()
        results_list = list(results)
        alerts_list = list(alerts)
        run_id = self._insert(
            "model_runs",
            {
                "user_id": user_id,
                "date": date,
                "model_version": model_version,
                "params_version": params_version,
                "input_json": _json_dumps(input_payload or {}),
                "s_end": final_state.get("S_end"),
                "e_end": final_state.get("E_end"),
                "s_star": final_state.get("S_star"),
                "s_threshold": final_state.get("S_threshold"),
                "alerts_count": len(alerts_list),
                "created_at": _utc_now(),
            },
        )
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO curve_points (run_id, time, stress, energy, state)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        run_id,
                        row.get("time"),
                        row.get("S"),
                        row.get("E"),
                        row.get("state"),
                    )
                    for row in results_list
                ],
            )
        return run_id

    def record_evaluation(
        self,
        metrics: Dict[str, Any],
        user_id: str = DEFAULT_USER_ID,
        params_version: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> int:
        self.init_schema()
        return self._insert(
            "evaluation_runs",
            {
                "user_id": user_id,
                "params_version": params_version,
                "sample_count": metrics.get("sample_count", 1),
                "stress_mae": metrics.get("stress_mae"),
                "energy_mae": metrics.get("energy_mae"),
                "trend_accuracy": metrics.get("trend_accuracy"),
                "peak_time_error_min": metrics.get("peak_time_error_min"),
                "alert_score": metrics.get("alert_score"),
                "total_loss": metrics.get("total_loss"),
                "metrics_json": _json_dumps(metrics),
                "notes": notes,
                "created_at": _utc_now(),
            },
        )

    def record_calibration_job(
        self,
        report: Dict[str, Any],
        user_id: str = DEFAULT_USER_ID,
        status: str = "completed",
        base_params_version: Optional[str] = None,
        best_params_version: Optional[str] = None,
    ) -> int:
        self.init_schema()
        return self._insert(
            "calibration_jobs",
            {
                "user_id": user_id,
                "base_params_version": base_params_version,
                "best_params_version": best_params_version,
                "status": status,
                "best_loss": report.get("best_loss"),
                "search_space_json": _json_dumps(report.get("search_space", {})),
                "report_json": _json_dumps(report),
                "started_at": report.get("started_at", _utc_now()),
                "ended_at": report.get("ended_at", _utc_now()),
            },
        )

    def list_daily_feedback(self, user_id: str = DEFAULT_USER_ID, limit: int = 30) -> list:
        self.init_schema()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM daily_feedback
                WHERE user_id = ?
                ORDER BY date DESC, id DESC
                LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def admin_reliability_overview(self) -> Dict[str, Any]:
        """Return evidence actually recorded by evaluation and calibration jobs."""
        self.init_schema()
        with self._connect() as conn:
            counts = {
                "daily_feedback": int(
                    conn.execute("SELECT COUNT(*) FROM daily_feedback").fetchone()[0]
                ),
                "event_feedback": int(
                    conn.execute("SELECT COUNT(*) FROM event_feedback").fetchone()[0]
                ),
                "evaluations": int(
                    conn.execute("SELECT COUNT(*) FROM evaluation_runs").fetchone()[0]
                ),
                "calibration_jobs": int(
                    conn.execute("SELECT COUNT(*) FROM calibration_jobs").fetchone()[0]
                ),
                "model_runs": int(
                    conn.execute("SELECT COUNT(*) FROM model_runs").fetchone()[0]
                ),
            }
            latest = conn.execute(
                """
                SELECT id, user_id, params_version, sample_count, stress_mae,
                       energy_mae, trend_accuracy, peak_time_error_min,
                       alert_score, total_loss, metrics_json, notes, created_at
                FROM evaluation_runs
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """
            ).fetchone()
            recent_evaluations = conn.execute(
                """
                SELECT id, user_id, params_version, sample_count, stress_mae,
                       energy_mae, trend_accuracy, peak_time_error_min,
                       alert_score, total_loss, created_at
                FROM evaluation_runs
                ORDER BY created_at DESC, id DESC
                LIMIT 20
                """
            ).fetchall()
            recent_jobs = conn.execute(
                """
                SELECT id, user_id, base_params_version, best_params_version,
                       status, best_loss, started_at, ended_at
                FROM calibration_jobs
                ORDER BY started_at DESC, id DESC
                LIMIT 10
                """
            ).fetchall()

        latest_payload = dict(latest) if latest else None
        if latest_payload:
            try:
                latest_payload["metrics"] = json.loads(
                    latest_payload.pop("metrics_json") or "{}"
                )
            except json.JSONDecodeError:
                latest_payload["metrics"] = {}

        sample_count = int(latest_payload["sample_count"]) if latest_payload else 0
        if sample_count >= 30:
            evidence_level = "sufficient"
        elif sample_count >= 7:
            evidence_level = "limited"
        else:
            evidence_level = "insufficient"

        return {
            "counts": counts,
            "latest_evaluation": latest_payload,
            "recent_evaluations": [dict(row) for row in recent_evaluations],
            "recent_jobs": [dict(row) for row in recent_jobs],
            "evidence_level": evidence_level,
            "evidence_note": (
                "样本量表示已参与误差评估的日级样本数；它不是医学置信度。"
            ),
        }

    def _insert(self, table: str, row: Dict[str, Any]) -> int:
        keys = list(row.keys())
        placeholders = ", ".join("?" for _ in keys)
        columns = ", ".join(keys)
        with self._connect() as conn:
            cursor = conn.execute(
                f"INSERT INTO {table} ({columns}) VALUES ({placeholders})",
                [row[key] for key in keys],
            )
            return int(cursor.lastrowid)
