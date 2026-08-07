from __future__ import annotations

import sys
import os

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '../'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from flask import Flask, g, render_template, request, jsonify, session, send_from_directory, redirect, url_for
from markupsafe import escape
import hmac
import hashlib
import json
import secrets
from datetime import date, datetime, timedelta
from typing import Any
from werkzeug.middleware.proxy_fix import ProxyFix

from auth.database import AppDatabase
from auth.security import (
    admin_required,
    auth_required,
    get_database,
    get_identity,
    session_required,
)
from entity.user import User
from utils.event_factory import EventFactory
from utils.get_token import FeishuAPI
from visualization.plotter import get_plot_image_base64
from data_pipeline.orchestrator import inject_routine_events
from algorithm.time_utils import normalize_interval, overlaps
from calibration.calibrator import calibrate_parameters
from calibration.metrics import evaluate_simulation
from calibration.model_comparison import run_nested_model_comparison
from calibration.care_frequency_validation import run_synthetic_care_frequency_check
from calibration.semantic_validation import run_numerical_semantic_check
from calibration.validation_protocol import run_engineering_validation_protocol
from calibration.parameter_validation import get_nested, set_nested, validate_params
from calibration.simulation_runner import run_simulation_for_calibration
from calibration.storage import CalibrationStore
from settings.model_defaults import (
    APP_DEFAULT_PORT,
    DEFAULT_CALLBACK_PATH,
    DEFAULT_INITIAL_ENERGY,
    FEISHU_REQUEST_TIMEOUT_SECONDS,
)
from services.onboarding import (
    FEATURE_VERSION,
    MAPPING_VERSION,
    MODEL_VERSION,
    PARAMETER_VERSION,
    QUESTIONNAIRE_DEFINITION,
    build_daily_context,
    build_routine_plan,
    infer_profile,
    new_id,
    utc_now,
    validate_and_normalize_submission,
)
from services.strategy_catalog import (
    build_strategy_curves,
    strategy_payload,
    validate_strategy_selection,
)
from services.cross_day_context import (
    build_automatic_cross_day_context,
    semantic_context_from_cross_day,
)
from services.event_semantics import semantic_agent_status
from integrations.feishu.identity import FeishuIdentityService
from services.care_service import CareService

template_dir = os.path.join(project_root, 'templates')
static_dir = os.path.join(project_root, 'static')
frontend_dist_dir = os.path.join(project_root, "frontend_dist")

app = Flask(__name__, template_folder=template_dir, static_folder=static_dir)
runtime_environment = os.getenv("APP_ENV", "development").strip().lower()
secret_key = os.getenv("FLASK_SECRET_KEY")
if runtime_environment == "production" and not secret_key:
    raise RuntimeError("FLASK_SECRET_KEY is required when APP_ENV=production")

app.config.update(
    SECRET_KEY=secret_key or secrets.token_hex(32),
    MAX_CONTENT_LENGTH=int(os.getenv("MAX_CONTENT_LENGTH", str(2 * 1024 * 1024))),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("SESSION_COOKIE_SECURE", "false").lower() == "true",
    PERMANENT_SESSION_LIFETIME=timedelta(
        hours=int(os.getenv("SESSION_LIFETIME_HOURS", "12"))
    ),
)

if os.getenv("TRUST_PROXY_HEADERS", "false").lower() == "true":
    app.wsgi_app = ProxyFix(
        app.wsgi_app,
        x_for=1,
        x_proto=1,
        x_host=1,
        x_port=1,
    )

application_database = AppDatabase()
application_database.init_schema()
application_database.save_questionnaire_definition(QUESTIONNAIRE_DEFINITION)
care_service = CareService(
    application_database,
    token_path_factory=lambda user_id: _feishu_token_path(user_id),
)
feishu_identity_service = FeishuIdentityService(
    application_database,
    app_id=os.getenv("FEISHU_APP_ID", ""),
    bind_base_url=os.getenv("FEISHU_BIND_BASE_URL", ""),
    token_ttl_seconds=int(os.getenv("FEISHU_BIND_TOKEN_TTL_SECONDS", "900")),
)

bootstrap_login_id = os.getenv("BOOTSTRAP_ADMIN_LOGIN_ID")
bootstrap_password = os.getenv("BOOTSTRAP_ADMIN_PASSWORD")
if bootstrap_login_id and bootstrap_password:
    if application_database.get_user_by_login_id(bootstrap_login_id) is None:
        application_database.create_user(
            bootstrap_login_id,
            bootstrap_password,
            role="admin",
        )


def _json_safe(value):
    """Convert tuple-keyed config dictionaries into JSON-safe payloads."""
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def _get_model_user() -> User:
    """Build one request-scoped simulation User from the authenticated profile."""
    cached = getattr(g, "model_user", None)
    if cached is not None:
        return cached
    identity = get_identity()
    if not identity:
        raise RuntimeError("authenticated identity is required")
    stored_params = get_database().load_user_params(identity["id"])
    params = User._params_from_json_safe(stored_params)
    g.model_user = User(
        user_id=str(identity["id"]),
        params=params,
        load_from_file=False,
    )
    return g.model_user


def _save_model_user(user: User) -> None:
    identity = get_identity()
    if not identity:
        raise RuntimeError("authenticated identity is required")
    get_database().save_user_params(
        identity["id"],
        User._params_to_json_safe(user.params),
    )


def _apply_profile_routine(user: User, profile: dict | None) -> None:
    """Apply routine facts to this request only; never rewrite model priors."""
    if not profile:
        return
    routine = profile.get("routine", {})
    if not isinstance(routine, dict):
        return
    cfg = dict(user.get_param("routine_weaver", {}) or {})
    lunch = routine.get("lunch_ideal_time")
    dinner = routine.get("dinner_ideal_time")
    if lunch:
        lunch_minutes = int(lunch[:2]) * 60 + int(lunch[3:])
        cfg["lunch_ideal_start"] = lunch
        cfg["lunch_ideal_end"] = f"{(lunch_minutes + 30) // 60:02d}:{(lunch_minutes + 30) % 60:02d}"
    if dinner:
        dinner_minutes = int(dinner[:2]) * 60 + int(dinner[3:])
        cfg["dinner_ideal_start"] = dinner
        cfg["dinner_ideal_end"] = f"{(dinner_minutes + 30) // 60:02d}:{(dinner_minutes + 30) % 60:02d}"
    if routine.get("weekday_wake_time"):
        user.params["default_wake_time"] = routine["weekday_wake_time"]
    if routine.get("weekday_sleep_start"):
        user.params["default_sleep_time"] = routine["weekday_sleep_start"]
    user.params["routine_weaver"] = cfg
    applied_priors = []
    mapping_is_current = profile.get("mapping_version") == MAPPING_VERSION
    allowed_prior_paths = set(
        QUESTIONNAIRE_DEFINITION.get("parameter_whitelist", [])
    )
    for prior in profile.get("parameter_priors", []):
        if not isinstance(prior, dict):
            continue
        path = str(prior.get("parameter") or "")
        if not mapping_is_current or path not in allowed_prior_paths:
            continue
        current = get_nested(user.params, path)
        if not path or current is None:
            continue
        try:
            group_mean = float(current)
            questionnaire_mean = float(prior["mean"])
        except (KeyError, TypeError, ValueError):
            continue
        # The questionnaire is a weak prior mean correction, not a permanent
        # direct assignment.  The request-scoped runtime value remains mostly
        # shrunk toward the versioned group parameter.
        runtime_value = 0.65 * group_mean + 0.35 * questionnaire_mean
        set_nested(user.params, path, runtime_value)
        applied_priors.append(
            {
                "parameter": path,
                "group_mean": group_mean,
                "questionnaire_prior_mean": questionnaire_mean,
                "runtime_prior_mean": runtime_value,
                "prior_strength": "weak",
            }
        )
    user.params["individual_parameter_priors"] = applied_priors
    if "ctssm" not in str(user.params.get("model_family", "")).lower():
        user._init_strategies()
    user.solver.update_user(user)


def _profile_for_response(profile: dict | None) -> dict | None:
    """Do not present or apply retired parameter priors as current CTSSM facts."""
    if not profile:
        return None
    presented = dict(profile)
    is_current = profile.get("mapping_version") == MAPPING_VERSION
    presented["mapping_is_current"] = is_current
    presented["current_mapping_version"] = MAPPING_VERSION
    if not is_current:
        presented["parameter_priors"] = []
        presented["mapping_notice"] = (
            "该画像来自旧版映射，仅保留历史参考；请重新填写以生成当前模型先验。"
        )
    return presented


def _event_windows(events) -> list:
    windows = []
    for event in events:
        try:
            start, end = normalize_interval(event.start_time, event.end_time)
            windows.append({"start": start, "end": end})
        except (AttributeError, TypeError, ValueError):
            continue
    return windows


def _stored_ema_observations(user_id: int, target_date: str) -> list[dict]:
    observations = []
    for item in application_database.list_feedback_observations(
        user_id,
        target_date=target_date,
        limit=200,
    ):
        if item.get("feedback_type") != "momentary_state":
            continue
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        target_time = item.get("target_time") or item.get("reported_at")
        recall_delay = 0.0
        try:
            target_dt = datetime.fromisoformat(str(target_time).replace("Z", "+00:00"))
            reported_dt = datetime.fromisoformat(
                str(item.get("reported_at") or target_time).replace("Z", "+00:00")
            )
            if target_dt.tzinfo is None and reported_dt.tzinfo is not None:
                reported_dt = reported_dt.replace(tzinfo=None)
            elif target_dt.tzinfo is not None and reported_dt.tzinfo is None:
                target_dt = target_dt.replace(tzinfo=None)
            recall_delay = max(0.0, (reported_dt - target_dt).total_seconds() / 60.0)
        except (TypeError, ValueError):
            recall_delay = 60.0 if item.get("retrospective") else 0.0
        observations.append(
            {
                "target_time": target_time,
                "stress": payload.get("stress_0_10"),
                "vitality": payload.get(
                    "vitality_0_10", payload.get("energy_0_10")
                ),
                "perseverative_cognition": payload.get(
                    "perseverative_cognition_0_10"
                ),
                "retrospective": bool(item.get("retrospective")),
                "recall_delay_minutes": recall_delay,
                "feedback_id": item.get("feedback_id"),
            }
        )
    return observations


def _is_legacy_strategy_config_key(key: Any) -> bool:
    normalized = str(key or "").lower()
    return (
        normalized == "legacy_model"
        or "strategy" in normalized
        or normalized.startswith("night_")
        or normalized.startswith("rest_")
        or normalized in {"time_pref_weights"}
    )


def _validate_mock_events(value) -> list:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("mock_events must be an array")
    allowed_types = {"course", "task", "rest", "gym", "library"}
    normalized = []
    for index, item in enumerate(value[:100]):
        if not isinstance(item, dict):
            raise ValueError(f"mock_events[{index}] must be an object")
        event_type = str(item.get("type") or "")
        name = str(item.get("name") or "").strip()[:120]
        start = str(item.get("start") or "")
        end = str(item.get("end") or "")
        if event_type not in allowed_types or not name:
            raise ValueError(f"mock_events[{index}] has an invalid type or name")
        try:
            start_time = datetime.strptime(start, "%H:%M")
            end_time = datetime.strptime(end, "%H:%M")
        except ValueError as exc:
            raise ValueError(f"mock_events[{index}] must use HH:MM") from exc
        if end_time <= start_time:
            raise ValueError(f"mock_events[{index}] must end after it starts")
        normalized.append({**item, "type": event_type, "name": name, "start": start, "end": end})
    return normalized


def _get_or_create_routine_context(user_id: int, target_date: str, events=None):
    profile = application_database.latest_profile_snapshot(user_id)
    if not profile:
        return None, None, None
    plan = application_database.get_routine_plan(user_id, target_date)
    if plan is None or events:
        plan = build_routine_plan(
            profile,
            target_date=target_date,
            occupied_windows=_event_windows(events or []),
        )
        application_database.save_routine_plan(user_id, plan)
    context = build_daily_context(profile, plan, target_date)
    application_database.save_daily_context(user_id, context)
    return profile, plan, context


def _feishu_token_path(user_id: int) -> str:
    """Return an account-isolated token path (legacy global tokens are ignored)."""
    token_dir = os.path.join(project_root, "data", "user_tokens")
    os.makedirs(token_dir, exist_ok=True)
    return os.path.join(token_dir, f"user_{int(user_id)}.json")


def _feishu_callback_page(success: bool, title: str, message: str, status_code=200):
    """Render a small OAuth result page and notify the Vue opener."""
    event_payload = json.dumps(
        {
            "type": "mindflow:feishu-oauth",
            "status": "success" if success else "error",
            "message": message,
        },
        ensure_ascii=False,
    ).replace("<", "\\u003c")
    frontend_origin = (
        os.getenv("FEISHU_FRONTEND_ORIGIN") or request.host_url.rstrip("/")
    )
    frontend_origin_json = json.dumps(frontend_origin).replace("<", "\\u003c")
    tone = "#315c4b" if success else "#9a4f43"
    html = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{escape(title)}</title>
  <style>
    body {{ margin:0; min-height:100vh; display:grid; place-items:center; background:#f4f0e7;
      color:#20352d; font-family:system-ui,-apple-system,"Segoe UI",sans-serif; }}
    main {{ width:min(420px,calc(100% - 40px)); padding:34px; border:1px solid #d8d2c4;
      border-radius:24px; background:#fffdf8; box-shadow:0 18px 60px rgba(35,56,47,.12); }}
    b {{ color:{tone}; font-size:1.25rem; }} p {{ line-height:1.75; color:#66746e; }}
  </style>
</head>
<body>
  <main><b>{escape(title)}</b><p>{escape(message)}</p><p>此窗口将自动关闭。</p></main>
  <script>
    if (window.opener) {{
      window.opener.postMessage({event_payload}, {frontend_origin_json});
    }}
    window.setTimeout(() => window.close(), 1200);
  </script>
</body>
</html>"""
    return html, status_code, {"Content-Type": "text/html; charset=utf-8"}


def _serve_vue_app():
    """Serve the production Vite build, with the legacy templates as fallback."""
    index_path = os.path.join(frontend_dist_dir, "index.html")
    if os.path.isfile(index_path):
        return send_from_directory(frontend_dist_dir, "index.html")
    return None


@app.route('/assets/<path:filename>')
def vue_assets(filename):
    """Serve hashed Vite production assets."""
    assets_dir = os.path.join(frontend_dist_dir, "assets")
    if not os.path.isdir(assets_dir):
        return jsonify(
            {
                "status": "error",
                "message": "Vue frontend is not built. Run npm run build.",
            }
        ), 404
    return send_from_directory(assets_dir, filename)


@app.route('/')
@auth_required
def index():
    """Vue application entry for authenticated users."""
    vue_response = _serve_vue_app()
    if vue_response is not None:
        return vue_response
    return render_template('index.html', auth_user=get_identity())


@app.route('/login', methods=['GET'])
def login_page():
    """Vue login entry; legacy template remains a no-build fallback."""
    if get_identity():
        return redirect("/")
    vue_response = _serve_vue_app()
    if vue_response is not None:
        return vue_response
    return render_template('login.html')


@app.route('/api/health', methods=['GET'])
def health():
    """Unauthenticated liveness/readiness endpoint for hosts and containers."""
    try:
        stats = application_database.stats()
        return jsonify(
            {
                "status": "ok",
                "service": "mental-health-simulator",
                "database": {
                    "ready": True,
                    "journal_mode": stats["journal_mode"],
                    "schema_version": stats["schema_version"],
                },
            }
        )
    except Exception:
        return jsonify(
            {
                "status": "error",
                "service": "mental-health-simulator",
                "database": {"ready": False},
            }
        ), 503


@app.route('/api/auth/login', methods=['POST'])
def auth_login():
    data = request.get_json(silent=True) or {}
    login_id = str(data.get("login_id") or "")
    password = str(data.get("password") or "")
    user = application_database.authenticate_password(
        login_id,
        password,
    )
    if not user:
        application_database.record_audit(
            "auth.login_failed",
            ip_address=request.remote_addr,
            details={
                "login_id_fingerprint": hashlib.sha256(
                    login_id.strip().lower().encode("utf-8")
                ).hexdigest()[:12]
            },
        )
        return jsonify(
            {
                "status": "error",
                "code": "invalid_credentials",
                "message": "邮箱、学号或密码错误",
            }
        ), 401
    session.clear()
    session["user_id"] = user["id"]
    session.permanent = True
    application_database.record_audit(
        "auth.login",
        user_id=user["id"],
        ip_address=request.remote_addr,
    )
    return jsonify({"status": "success", "user": user})


@app.route('/api/auth/register', methods=['POST'])
def auth_register():
    """Create a standard user account and start a session."""
    data = request.get_json(silent=True) or {}
    try:
        user = application_database.create_user(
            data.get("login_id", ""),
            data.get("password", ""),
            role="user",
        )
    except (TypeError, ValueError) as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    session.clear()
    session["user_id"] = user["id"]
    session.permanent = True
    application_database.record_audit(
        "auth.register",
        user_id=user["id"],
        ip_address=request.remote_addr,
    )
    return jsonify({"status": "success", "user": user}), 201


@app.route('/api/auth/logout', methods=['POST'])
@session_required
def auth_logout():
    identity = get_identity()
    application_database.record_audit(
        "auth.logout",
        user_id=identity["id"],
        ip_address=request.remote_addr,
    )
    session.clear()
    return jsonify({"status": "success"})


@app.route('/api/auth/me', methods=['GET'])
@auth_required
def auth_me():
    identity = dict(get_identity())
    identity.pop("api_key_id", None)
    return jsonify({"status": "success", "user": identity})


@app.route('/api/auth/api-keys', methods=['GET', 'POST'])
@session_required
def auth_api_keys():
    identity = get_identity()
    if request.method == "GET":
        return jsonify(
            {
                "status": "success",
                "api_keys": application_database.list_api_keys(identity["id"]),
            }
        )
    data = request.get_json(silent=True) or {}
    try:
        result = application_database.create_api_key(
            identity["id"],
            data.get("name", ""),
            expires_days=data.get("expires_days"),
        )
    except (TypeError, ValueError) as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    application_database.record_audit(
        "api_key.create",
        user_id=identity["id"],
        ip_address=request.remote_addr,
        details={"api_key_id": result["id"], "name": result["name"]},
    )
    return jsonify(
        {
            "status": "success",
            "api_key": result,
            "warning": "密钥只显示这一次，请立即安全保存。",
        }
    ), 201


@app.route('/api/auth/api-keys/<int:key_id>', methods=['DELETE'])
@session_required
def auth_revoke_api_key(key_id):
    identity = get_identity()
    if not application_database.revoke_api_key(key_id, user_id=identity["id"]):
        return jsonify({"status": "error", "message": "API Key 不存在或已撤销"}), 404
    application_database.record_audit(
        "api_key.revoke",
        user_id=identity["id"],
        ip_address=request.remote_addr,
        details={"api_key_id": key_id},
    )
    return jsonify({"status": "success"})


@app.route('/api/admin/users', methods=['GET', 'POST'])
@admin_required
def admin_users():
    if request.method == "GET":
        return jsonify({"status": "success", "users": application_database.list_users()})
    data = request.get_json(silent=True) or {}
    try:
        created = application_database.create_user(
            data.get("login_id", ""),
            data.get("password", ""),
            role=data.get("role", "user"),
        )
    except (TypeError, ValueError) as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    application_database.record_audit(
        "admin.user_create",
        user_id=get_identity()["id"],
        ip_address=request.remote_addr,
        details={"created_user_id": created["id"], "role": created["role"]},
    )
    return jsonify({"status": "success", "user": created}), 201


@app.route('/api/admin/users/<int:user_id>/active', methods=['PATCH'])
@admin_required
def admin_set_user_active(user_id):
    data = request.get_json(silent=True) or {}
    if not isinstance(data.get("is_active"), bool):
        return jsonify({"status": "error", "message": "is_active must be boolean"}), 400
    if user_id == get_identity()["id"] and not data["is_active"]:
        return jsonify({"status": "error", "message": "不能停用当前管理员自己"}), 400
    try:
        updated = application_database.set_user_active(user_id, data["is_active"])
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 404
    application_database.record_audit(
        "admin.user_active",
        user_id=get_identity()["id"],
        ip_address=request.remote_addr,
        details={"target_user_id": user_id, "is_active": data["is_active"]},
    )
    return jsonify({"status": "success", "user": updated})


@app.route('/api/admin/database/stats', methods=['GET'])
@admin_required
def admin_database_stats():
    stats = application_database.stats()
    stats.pop("path", None)
    return jsonify({"status": "success", "database": stats})


@app.route('/api/admin/overview', methods=['GET'])
@admin_required
def admin_overview():
    """Management view grounded in persisted application and evaluation data."""
    database = application_database.stats()
    database.pop("path", None)
    return jsonify(
        _json_safe(
            {
                "status": "success",
                "application": application_database.admin_overview(),
                "reliability": CalibrationStore().admin_reliability_overview(),
                "database": database,
                "versions": {
                    "model": MODEL_VERSION,
                    "parameters": PARAMETER_VERSION,
                    "features": FEATURE_VERSION,
                },
                "reliability_notice": (
                    "可靠性只依据已保存的反馈与误差评估展示；样本不足时不会生成置信度。"
                ),
            }
        )
    )


@app.route('/api/admin/prediction-runs', methods=['GET'])
@admin_required
def admin_prediction_runs():
    try:
        limit = int(request.args.get("limit", 30))
        user_id = request.args.get("user_id")
        selected_user_id = int(user_id) if user_id not in (None, "") else None
    except (TypeError, ValueError):
        return jsonify({"status": "error", "message": "invalid list filters"}), 400
    return jsonify(
        {
            "status": "success",
            "runs": application_database.admin_prediction_runs(
                limit=limit,
                user_id=selected_user_id,
            ),
        }
    )


@app.route('/api/admin/prediction-runs/<prediction_run_id>', methods=['GET'])
@admin_required
def admin_prediction_run_detail(prediction_run_id):
    run = application_database.admin_prediction_run_detail(prediction_run_id)
    if run is None:
        return jsonify({"status": "error", "message": "预测运行不存在"}), 404
    return jsonify(_json_safe({"status": "success", "run": run}))


@app.route('/api/admin/audit-logs', methods=['GET'])
@admin_required
def admin_audit_logs():
    try:
        limit = int(request.args.get("limit", 40))
    except (TypeError, ValueError):
        return jsonify({"status": "error", "message": "invalid limit"}), 400
    return jsonify(
        {
            "status": "success",
            "audit_logs": application_database.recent_audit_logs(limit),
        }
    )


@app.route('/api/admin/model/curves', methods=['GET'])
@admin_required
def admin_model_curves():
    """Compare real strategy functions with deterministic diagnostic inputs."""
    if str(request.args.get("legacy") or "").lower() not in {"1", "true", "yes"}:
        return jsonify(
            {
                "status": "error",
                "message": "旧策略函数不属于 CTSSM；仅显式 legacy=true 时可作历史基线诊断",
            }
        ), 410
    family = str(request.args.get("family") or "f_strategy")
    target_user_id = request.args.get("user_id")
    if target_user_id not in (None, ""):
        try:
            user_id = int(target_user_id)
        except (TypeError, ValueError):
            return jsonify({"status": "error", "message": "invalid user_id"}), 400
        if application_database.get_user(user_id) is None:
            return jsonify({"status": "error", "message": "用户不存在"}), 404
        stored_params = application_database.load_user_params(user_id)
        params = User._params_from_json_safe(stored_params)
    else:
        params = _get_model_user().params
    try:
        curves = build_strategy_curves(
            params,
            family,
            stress=request.args.get("stress"),
            energy=request.args.get("energy"),
            baseline=request.args.get("baseline"),
        )
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    return jsonify(_json_safe({"status": "success", "curves": curves}))


@app.route('/api/feishu/get_url', methods=['GET'])
@session_required
def feishu_get_url():
    """Return an OAuth URL, or reuse the current valid authorization."""
    try:
        api = FeishuAPI(require_secret=False)
        user_id = int(get_identity()["id"])
        force = request.args.get("force", "").strip().lower() in {"1", "true", "yes"}
        connection = api.get_connection_status(
            _feishu_token_path(user_id),
            refresh=bool(api.app_secret),
        )
        if connection.get("valid") and not force:
            return jsonify({
                "status": "success",
                "already_connected": True,
                "connection": connection,
            })

        oauth_state = secrets.token_urlsafe(24)
        session["feishu_oauth_state"] = oauth_state
        session["feishu_oauth_user_id"] = user_id
        url = api.generate_authorize_url(state=oauth_state)
        missing = []
        if not api.app_id:
            missing.append("FEISHU_APP_ID")
        if not os.getenv("FEISHU_APP_SECRET") and not os.getenv("APP_SECRET"):
            missing.append("FEISHU_APP_SECRET")
        return jsonify({
            "status": "success",
            "url": url,
            "redirect_uri": api.redirect_uri,
            "missing": missing,
            "already_connected": False,
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400

@app.route(DEFAULT_CALLBACK_PATH, methods=['GET'])
def feishu_callback():
    """Exchange OAuth code and persist the rotating token for this app user."""
    code = request.args.get("code", "").strip()
    error = request.args.get("error", "").strip()
    error_description = request.args.get("error_description", "").strip()
    expected_state = session.pop("feishu_oauth_state", "")
    oauth_user_id = session.pop("feishu_oauth_user_id", None)
    received_state = request.args.get("state", "").strip()
    if (
        not expected_state
        or not received_state
        or oauth_user_id is None
        or not hmac.compare_digest(expected_state, received_state)
    ):
        return _feishu_callback_page(
            False,
            "飞书授权失败",
            "OAuth state 校验失败，请返回应用重新发起授权。",
            400,
        )
    if error:
        return _feishu_callback_page(
            False,
            "飞书授权失败",
            error_description or error,
            400,
        )
    if not code:
        return _feishu_callback_page(
            False,
            "飞书回调缺少 code",
            "请返回应用重新点击连接日历。",
            400,
        )

    try:
        api = FeishuAPI()
        token_info = api.get_user_access_token(code)
        api.save_token_to_file(token_info, _feishu_token_path(oauth_user_id))
        if token_info.get("refresh_token"):
            message = "已安全保存授权，后续过期时会自动刷新，无需手工填写 token。"
        else:
            message = "授权已保存，但未取得 refresh_token；请在飞书后台启用 offline_access 后重新授权。"
        return _feishu_callback_page(
            True,
            "飞书授权成功",
            message,
        )
    except Exception as e:
        return _feishu_callback_page(
            False,
            "飞书 Token 换取失败",
            f"{str(e)}。请确认飞书后台重定向 URL 与 FEISHU_REDIRECT_URI 完全一致。",
            500,
        )

@app.route('/api/feishu/submit_code', methods=['POST'])
@session_required
def feishu_submit_code():
    """Reject the legacy manual-code flow; OAuth callbacks must validate state."""
    return jsonify({
        "status": "error",
        "message": "手工提交授权码已停用，请直接点击“连接日历”完成飞书授权。",
    }), 410


@app.route('/feishu/bind', methods=['GET'])
@auth_required
def feishu_bind_page():
    """Render an explicit, session-authenticated one-time binding confirmation."""
    identity = get_identity()
    if not identity or identity.get("auth_type") != "session":
        return redirect(url_for("login_page", next=request.full_path))
    raw_token = str(request.args.get("token") or "")
    token_json = json.dumps(raw_token, ensure_ascii=False).replace("<", "\\u003c")
    if len(raw_token) < 32:
        return _feishu_callback_page(
            False,
            "绑定链接无效",
            "请回到飞书机器人重新获取绑定卡片。",
            400,
        )
    html = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>确认绑定飞书机器人</title>
  <style>
    body {{ margin:0; min-height:100vh; display:grid; place-items:center; background:#f4f0e7;
      color:#20352d; font-family:system-ui,-apple-system,"Segoe UI",sans-serif; }}
    main {{ width:min(460px,calc(100% - 40px)); padding:34px; border:1px solid #d8d2c4;
      border-radius:24px; background:#fffdf8; box-shadow:0 18px 60px rgba(35,56,47,.12); }}
    h1 {{ font-size:1.4rem; }} p {{ line-height:1.75; color:#66746e; }}
    button {{ border:0; border-radius:999px; padding:12px 20px; background:#315c4b; color:white;
      font:inherit; cursor:pointer; }} button:disabled {{ opacity:.55; cursor:wait; }}
    #result {{ min-height:1.5em; color:#315c4b; }}
  </style>
</head>
<body>
  <main>
    <h1>确认绑定飞书机器人</h1>
    <p>确认后，当前登录的 Mental_project 账号会与刚才发消息的飞书账号绑定。机器人随后只能读取这个项目账号自己的状态、预测和日历授权；你可以随时在 Web 设置中解绑。</p>
    <p>绑定飞书身份不会自动开启主动关怀，也不会自动同意外部 LLM 使用个人历史。</p>
    <button id="confirm" type="button">确认绑定</button>
    <p id="result" role="status"></p>
  </main>
  <script>
    const token = {token_json};
    const button = document.getElementById("confirm");
    const result = document.getElementById("result");
    button.addEventListener("click", async () => {{
      button.disabled = true;
      result.textContent = "正在确认…";
      try {{
        const response = await fetch("/api/feishu/bindings/confirm", {{
          method: "POST",
          headers: {{"Content-Type": "application/json"}},
          credentials: "same-origin",
          body: JSON.stringify({{token}}),
        }});
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.message || "绑定失败");
        result.textContent = "绑定成功。现在可以回到飞书继续使用。";
        button.remove();
      }} catch (error) {{
        result.textContent = error.message || "绑定失败，请重新获取绑定卡片。";
        button.disabled = false;
      }}
    }});
  </script>
</body>
</html>"""
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route('/api/feishu/bindings/confirm', methods=['POST'])
@session_required
def feishu_binding_confirm():
    data = request.get_json(silent=True) or {}
    try:
        binding = feishu_identity_service.confirm_binding(
            str(data.get("token") or ""),
            int(get_identity()["id"]),
        )
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    application_database.record_audit(
        "feishu_bot_binding_confirmed",
        user_id=int(get_identity()["id"]),
        ip_address=request.remote_addr,
        details={"binding_id": binding["id"], "app_id": binding["app_id"]},
    )
    return jsonify({
        "status": "success",
        "binding": feishu_identity_service.status_for_user(int(get_identity()["id"])),
    })


@app.route('/api/feishu/bindings/status', methods=['GET'])
@auth_required
def feishu_binding_status():
    return jsonify({
        "status": "success",
        "configured": bool(feishu_identity_service.app_id),
        "binding": feishu_identity_service.status_for_user(int(get_identity()["id"])),
    })


@app.route('/api/feishu/bindings/current', methods=['DELETE'])
@session_required
def feishu_binding_delete():
    user_id = int(get_identity()["id"])
    revoked = feishu_identity_service.revoke_binding(user_id)
    if revoked:
        application_database.record_audit(
            "feishu_bot_binding_revoked",
            user_id=user_id,
            ip_address=request.remote_addr,
            details={},
        )
    return jsonify({"status": "success", "revoked": revoked})


@app.route('/api/care/preferences', methods=['GET', 'PATCH'])
@auth_required
def care_preferences():
    user_id = int(get_identity()["id"])
    if request.method == 'GET':
        return jsonify({
            "status": "success",
            "preferences": care_service.get_preferences(user_id),
        })
    changes = request.get_json(silent=True) or {}
    if not isinstance(changes, dict):
        return jsonify({"status": "error", "message": "请求体必须是对象"}), 400
    before = care_service.get_preferences(user_id)
    try:
        preferences = care_service.update_preferences(user_id, changes)
    except (TypeError, ValueError) as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    audited_keys = {
        "feishu_proactive_enabled",
        "allow_personal_history_reference",
        "allow_external_llm",
    }
    changed_audited = sorted(
        key for key in audited_keys
        if key in changes and before.get(key) != preferences.get(key)
    )
    if changed_audited:
        application_database.record_audit(
            "care_preferences_consent_changed",
            user_id=user_id,
            ip_address=request.remote_addr,
            details={"fields": changed_audited},
        )
    return jsonify({"status": "success", "preferences": preferences})


@app.route('/api/admin/feishu-bot/status', methods=['GET'])
@admin_required
def admin_feishu_bot_status():
    return jsonify({"status": "success", **application_database.feishu_bot_status()})


@app.route('/api/admin/feishu-bot/failures', methods=['GET'])
@admin_required
def admin_feishu_bot_failures():
    return jsonify({
        "status": "success",
        "failures": application_database.feishu_bot_failures(
            limit=request.args.get("limit", 50, type=int)
        ),
    })


@app.route('/api/admin/feishu-bot/failures/<event_id>/retry', methods=['POST'])
@admin_required
def admin_retry_feishu_bot_failure(event_id):
    retried = application_database.retry_failed_feishu_event(str(event_id))
    if not retried:
        return jsonify({"status": "error", "message": "失败事件不存在或当前不可重试"}), 404
    application_database.record_audit(
        "feishu_bot_failure_retried",
        user_id=int(get_identity()["id"]),
        ip_address=request.remote_addr,
        details={"event_id": str(event_id)},
    )
    return jsonify({"status": "success", "retried": True})


@app.route('/api/onboarding/questionnaire', methods=['GET'])
@auth_required
def onboarding_questionnaire():
    return jsonify({"status": "success", "questionnaire": QUESTIONNAIRE_DEFINITION})


@app.route('/api/onboarding/status', methods=['GET'])
@auth_required
def onboarding_status():
    user_id = int(get_identity()["id"])
    profile = application_database.latest_profile_snapshot(user_id)
    response = application_database.latest_questionnaire_response(user_id)
    return jsonify(
        {
            "status": "success",
            "completed": profile is not None,
            "questionnaire_version": (
                response["questionnaire_version"] if response else None
            ),
            "submitted_at": response["submitted_at"] if response else None,
            "profile": profile,
        }
    )


@app.route('/api/onboarding/responses', methods=['POST'])
@session_required
def onboarding_responses():
    user_id = int(get_identity()["id"])
    try:
        response = validate_and_normalize_submission(
            request.get_json(silent=True) or {}
        )
        inference_run, profile = infer_profile(response)
        routine_plan = build_routine_plan(profile, target_date=date.today().isoformat())
        daily_context = build_daily_context(
            profile,
            routine_plan,
            date.today().isoformat(),
        )
        application_database.save_onboarding_bundle(
            user_id,
            response,
            inference_run,
            profile,
            routine_plan,
            daily_context,
        )
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    application_database.record_audit(
        "onboarding.submit",
        user_id=user_id,
        ip_address=request.remote_addr,
        details={
            "questionnaire_version": response["questionnaire_version"],
            "mapping_version": inference_run["mapping_version"],
        },
    )
    return jsonify(
        {
            "status": "success",
            "profile": profile,
            "routine_plan": routine_plan,
            "inference": {
                "mapping_version": inference_run["mapping_version"],
                "global_quality": inference_run["global_quality"],
            },
        }
    ), 201


@app.route('/api/profile', methods=['GET'])
@auth_required
def current_profile():
    profile = application_database.latest_profile_snapshot(int(get_identity()["id"]))
    if profile is None:
        return jsonify(
            {
                "status": "error",
                "code": "onboarding_required",
                "message": "请先完成初始化问卷",
            }
        ), 404
    return jsonify({"status": "success", "profile": _profile_for_response(profile)})


@app.route('/api/profile/strategies', methods=['GET', 'PATCH'])
@auth_required
def profile_strategies():
    """Keep the historical endpoint from masquerading as CTSSM personalization."""
    current_user = _get_model_user()
    if "ctssm" in str(current_user.params.get("model_family", "")).lower():
        return jsonify(
            {
                "status": "error",
                "message": "新模型不使用离散人格策略；请通过事件评价和 EMA 提供个体信息",
                "replacement": {
                    "event_appraisal": True,
                    "momentary_feedback": True,
                    "questionnaire_priors": "weak_bounded_priors",
                },
            }
        ), 410
    if request.method == "GET":
        return jsonify(
            {
                "status": "success",
                "strategies": strategy_payload(current_user.params),
            }
        )

    data = request.get_json(silent=True) or {}
    try:
        updates = validate_strategy_selection(data.get("strategies"))
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400

    current_user.update_strategy_config(
        f_strategy=updates.get("f_strategy"),
        C_strategy=updates.get("C_strategy"),
        night_strategy=updates.get("night_strategy"),
        rest_strategy=updates.get("rest_strategy"),
        time_preferences=current_user.get_param("time_preferences", []),
    )
    _save_model_user(current_user)
    application_database.record_audit(
        "profile.strategies_update",
        user_id=int(get_identity()["id"]),
        ip_address=request.remote_addr,
        details={"strategies": updates},
    )
    return jsonify(
        {
            "status": "success",
            "strategies": strategy_payload(current_user.params),
        }
    )


@app.route('/api/routine-plan', methods=['GET'])
@auth_required
def routine_plan():
    target_date = str(request.args.get("date") or date.today().isoformat())
    try:
        datetime.strptime(target_date, "%Y-%m-%d")
    except ValueError:
        return jsonify({"status": "error", "message": "date must use YYYY-MM-DD"}), 400
    user_id = int(get_identity()["id"])
    profile = application_database.latest_profile_snapshot(user_id)
    if profile is None:
        return jsonify(
            {"status": "error", "code": "onboarding_required", "message": "请先完成初始化问卷"}
        ), 404
    plan = application_database.get_routine_plan(user_id, target_date)
    if plan is None:
        plan = build_routine_plan(profile, target_date)
        application_database.save_routine_plan(user_id, plan)
    return jsonify({"status": "success", "routine_plan": plan})


@app.route('/api/dashboard', methods=['GET'])
@auth_required
def dashboard():
    user_id = int(get_identity()["id"])
    today = date.today().isoformat()
    profile = application_database.latest_profile_snapshot(user_id)
    plan = application_database.get_routine_plan(user_id, today)
    if profile and plan is None:
        plan = build_routine_plan(profile, today)
        application_database.save_routine_plan(user_id, plan)
    return jsonify(
        {
            "status": "success",
            "user": get_identity(),
            "onboarding_completed": profile is not None,
            "profile": _profile_for_response(profile),
            "routine_plan": plan,
            "recent_runs": application_database.recent_prediction_runs(user_id),
            "versions": {
                "model": MODEL_VERSION,
                "parameters": PARAMETER_VERSION,
                "features": FEATURE_VERSION,
            },
        }
    )


@app.route('/api/feedback', methods=['POST'])
@auth_required
def feedback_observation():
    data = request.get_json(silent=True) or {}
    authenticated_user_id = int(get_identity()["id"])
    feedback_type = str(data.get("feedback_type") or "")
    allowed_types = {
        "momentary_state",
        "peak_review",
        "event_impact",
        "prediction_review",
        "care_review",
        "routine_correction",
        "event_completion",
    }
    if feedback_type not in allowed_types:
        return jsonify({"status": "error", "message": "unsupported feedback_type"}), 400
    payload = data.get("payload")
    if not isinstance(payload, dict) or not payload:
        return jsonify({"status": "error", "message": "payload is required"}), 400
    if feedback_type == "momentary_state":
        required = {
            "stress_0_10",
            "activity",
            "stress_event_since_last",
            "event_ongoing",
        }
        missing = sorted(
            key for key in required if key not in payload or payload.get(key) in (None, "")
        )
        if "vitality_0_10" not in payload and "energy_0_10" not in payload:
            missing.append("vitality_0_10")
        if missing:
            return jsonify(
                {
                    "status": "error",
                    "message": "momentary_state 缺少论文最低 EMA 字段",
                    "fields": sorted(set(missing)),
                }
            ), 400
        for key in (
            "stress_0_10",
            "vitality_0_10",
            "energy_0_10",
            "perseverative_cognition_0_10",
        ):
            if key not in payload:
                continue
            try:
                value = float(payload[key])
            except (TypeError, ValueError):
                return jsonify({"status": "error", "message": f"{key} must be numeric"}), 400
            if not 0.0 <= value <= 10.0:
                return jsonify({"status": "error", "message": f"{key} must be 0-10"}), 400
    if feedback_type == "event_completion":
        if not isinstance(payload.get("completed"), bool):
            return jsonify(
                {"status": "error", "message": "event_completion.completed must be boolean"}
            ), 400
        if not str(payload.get("event_id") or payload.get("event_name") or "").strip():
            return jsonify(
                {
                    "status": "error",
                    "message": "event_completion requires event_id or event_name",
                }
            ), 400
    prediction_run_id = data.get("prediction_run_id")
    if prediction_run_id and not application_database.user_owns_prediction_run(
        authenticated_user_id,
        prediction_run_id,
    ):
        return jsonify(
            {"status": "error", "message": "关联的预测运行不存在"}
        ), 400
    feedback = {
        "feedback_id": new_id(),
        "schema_version": "feedback_observation.v2",
        "prediction_run_id": prediction_run_id,
        "feedback_type": feedback_type,
        "target_time": data.get("target_time"),
        "payload": payload,
        "reported_at": utc_now(),
        "retrospective": bool(data.get("retrospective", False)),
    }
    try:
        application_database.save_feedback_observation(
            authenticated_user_id,
            feedback,
        )
    except Exception as exc:
        if "FOREIGN KEY" in str(exc).upper():
            return jsonify({"status": "error", "message": "关联的预测运行不存在"}), 400
        raise
    return jsonify({"status": "success", "feedback_id": feedback["feedback_id"]}), 201


@app.route('/api/semantic-agent/status', methods=['GET'])
@auth_required
def event_semantic_agent_status():
    """Expose readiness and version metadata without ever returning the key."""

    return jsonify({"status": "success", **semantic_agent_status()})


@app.route('/api/prediction-runs/<prediction_run_id>/replay', methods=['POST'])
@auth_required
def replay_prediction_run(prediction_run_id):
    """Return the exact stored trajectory without API calls or re-inference."""

    authenticated_user_id = int(get_identity()["id"])
    run = application_database.prediction_run_detail_for_user(
        authenticated_user_id,
        prediction_run_id,
    )
    if run is None:
        return jsonify({"status": "error", "message": "预测运行不存在"}), 404
    frozen_result = dict(run.get("result") or {})
    stored_fingerprint = frozen_result.pop("fingerprint", None)
    fingerprint_payload = json.dumps(
        {"input": run.get("input") or {}, "result": frozen_result},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    verified_fingerprint = hashlib.sha256(fingerprint_payload).hexdigest()
    return jsonify(
        _json_safe(
            {
                "status": "success",
                "replay_mode": "frozen_stored_trajectory",
                "source_prediction_run_id": prediction_run_id,
                "local_date": run.get("local_date"),
                "versions": {
                    "schema": run.get("schema_version"),
                    "model": run.get("model_version"),
                    "parameters": run.get("parameter_version"),
                    "features": run.get("feature_version"),
                },
                "random_seed": run.get("random_seed"),
                "input": run.get("input"),
                "result": run.get("result"),
                "results": run.get("points", []),
                "diagnostics": run.get("diagnostics"),
                "stored_fingerprint": stored_fingerprint,
                "fingerprint_verified": bool(
                    stored_fingerprint
                    and hmac.compare_digest(
                        str(stored_fingerprint),
                        verified_fingerprint,
                    )
                ),
                "external_api_called": False,
            }
        )
    )


@app.route('/api/config', methods=['GET', 'POST'])
@auth_required
def handle_config():
    """Read or update paper-aligned parameters; legacy strategies stay hidden."""
    current_user = _get_model_user()
    if request.method == 'GET':
        safe_params = User._params_to_json_safe(
            {
                key: value
                for key, value in current_user.params.items()
                if not _is_legacy_strategy_config_key(key)
            }
        )
        return jsonify({"user_id": current_user.user_id, "params": safe_params})
    elif request.method == 'POST':
        data = request.get_json(silent=True) or {}
        new_params = data.get("params", {})
        if not isinstance(new_params, dict):
            return jsonify({"status": "error", "message": "params must be an object"}), 400
        deprecated = sorted(
            key for key in new_params if _is_legacy_strategy_config_key(key)
        )
        if deprecated:
            return jsonify(
                {
                    "status": "error",
                    "message": "CTSSM 不接受旧离散策略配置",
                    "fields": deprecated,
                }
            ), 410
        if "model_family" in new_params or "model_selection" in new_params:
            return jsonify(
                {
                    "status": "error",
                    "message": "候选模型只能通过版本化的时间外验证流程发布",
                }
            ), 403
        validation = validate_params({**current_user.params, **new_params})
        if not validation["valid"]:
            return jsonify(
                {
                    "status": "error",
                    "message": "参数校验失败",
                    "validation": validation,
                }
            ), 400
        current_user.update_params(new_params)
        _save_model_user(current_user)
        return jsonify({"status": "success", "message": "配置已保存到当前用户档案"})

@app.route('/api/params/validate', methods=['POST'])
@auth_required
def validate_runtime_params():
    """Validate a params payload before simulation or calibration."""
    data = request.json or {}
    current_user = _get_model_user()
    params = data.get("params", current_user.params)
    return jsonify({"status": "success", "validation": validate_params(params)})

@app.route('/api/feedback/daily', methods=['POST'])
@auth_required
def save_daily_feedback():
    """Store one day's lightweight self-report feedback in SQLite."""
    data = request.json or {}
    if not data.get("date"):
        return jsonify({"status": "error", "message": "date is required"}), 400
    row_id = CalibrationStore().record_daily_feedback(
        data,
        user_id=str(get_identity()["id"]),
    )
    return jsonify({"status": "success", "id": row_id})

@app.route('/api/feedback/event', methods=['POST'])
@auth_required
def save_event_feedback():
    """Store event-level correction feedback for classification and intensity."""
    data = request.json or {}
    if not data.get("date"):
        return jsonify({"status": "error", "message": "date is required"}), 400
    row_id = CalibrationStore().record_event_feedback(
        data,
        user_id=str(get_identity()["id"]),
    )
    return jsonify({"status": "success", "id": row_id})

@app.route('/api/evaluate', methods=['POST'])
@auth_required
def evaluate_curve():
    """Evaluate simulated or supplied curve results against feedback anchors."""
    data = request.json or {}
    current_user = _get_model_user()
    feedback = data.get("feedback", {})
    if not feedback:
        return jsonify({"status": "error", "message": "feedback is required"}), 400

    if data.get("results") is not None:
        results = data.get("results", [])
        alerts = data.get("alerts", [])
        simulation = None
    else:
        date_str = data.get("date")
        if not date_str:
            return jsonify({"status": "error", "message": "date is required when results are not supplied"}), 400
        simulation = run_simulation_for_calibration(
            date_str=date_str,
            events_json=data.get("events", []),
            user_params=data.get("user_profile", current_user.params),
            yesterday_state=data.get("yesterday_state"),
            weave_routines=data.get("weave_routines", True),
        )
        results = simulation["results"]
        alerts = simulation["alerts"]

    metrics = evaluate_simulation(results, alerts, feedback)
    if data.get("store"):
        store = CalibrationStore()
        authenticated_user_id = str(get_identity()["id"])
        store.record_evaluation(
            metrics,
            user_id=authenticated_user_id,
            notes=data.get("notes"),
        )
        if simulation is not None:
            store.record_model_run(
                date=data.get("date"),
                results=simulation["results"],
                final_state=simulation["final_state"],
                alerts=simulation["alerts"],
                user_id=authenticated_user_id,
                input_payload={"events": data.get("events", []), "feedback": feedback},
            )

    payload = {"status": "success", "metrics": metrics}
    if simulation is not None:
        payload["final_state"] = simulation["final_state"]
        payload["alerts"] = simulation["alerts"]
        if data.get("include_results"):
            payload["results"] = simulation["results"]
    return jsonify(_json_safe(payload))

@app.route('/api/calibrate', methods=['POST'])
@auth_required
def calibrate_curve_params():
    """Run lightweight local parameter calibration against feedback samples."""
    data = request.json or {}
    samples = data.get("samples", [])
    if not samples:
        return jsonify({"status": "error", "message": "samples is required"}), 400
    iterations = max(1, min(300, int(data.get("iterations", 60))))
    current_user = _get_model_user()
    base_params = data.get("base_params", current_user.params)
    report = calibrate_parameters(
        samples=samples,
        base_params=base_params,
        search_space=data.get("search_space"),
        iterations=iterations,
        seed=int(data.get("seed", 42)),
    )

    if data.get("store"):
        store = CalibrationStore()
        authenticated_user_id = str(get_identity()["id"])
        version_name = data.get("version_name", f"calibrated_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
        store.record_parameter_version(
            report["best_params"],
            version_name=version_name,
            user_id=authenticated_user_id,
            parent_version=data.get("base_params_version"),
            notes=data.get("notes"),
        )
        store.record_calibration_job(
            report,
            user_id=authenticated_user_id,
            base_params_version=data.get("base_params_version"),
            best_params_version=version_name,
        )

    return jsonify(_json_safe({"status": "success", "report": report}))


@app.route('/api/models/compare', methods=['POST'])
@auth_required
def compare_nested_models():
    """Run the complete-date M0 baseline through M4-readiness comparison."""

    data = request.get_json(silent=True) or {}
    samples = data.get("samples")
    if not isinstance(samples, list) or not samples:
        return jsonify(
            {
                "status": "error",
                "message": "samples must contain dated longitudinal observations",
            }
        ), 400
    current_user = _get_model_user()
    try:
        report = run_nested_model_comparison(
            samples,
            current_user.params,
            holdout_fraction=float(data.get("holdout_fraction", 0.30)),
        )
    except (TypeError, ValueError) as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    CalibrationStore().record_model_comparison(
        report,
        user_id=str(get_identity()["id"]),
    )
    return jsonify(
        _json_safe(
            {
                "status": "success",
                "report": report,
                "model_changed": False,
                "note": "结果已保存；只有全部保留门槛通过后才允许另行发布模型版本。",
            }
        )
    )


@app.route('/api/models/validate-engineering', methods=['POST'])
@auth_required
def validate_model_engineering_protocol():
    """Run required counterfactual checks without calling them empirical proof."""

    data = request.get_json(silent=True) or {}
    sample = data.get("sample")
    if not isinstance(sample, dict) or not sample.get("date"):
        return jsonify(
            {"status": "error", "message": "sample with date and events is required"}
        ), 400
    try:
        report = run_engineering_validation_protocol(
            sample,
            _get_model_user().params,
            model_variant=data.get("model_variant", "m0"),
        )
    except (TypeError, ValueError) as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    return jsonify(_json_safe({"status": "success", "report": report}))


@app.route('/api/models/validate-numerics', methods=['POST'])
@auth_required
def validate_model_numerics():
    """Run reproducible semantic and care-burden engineering checks."""

    data = request.get_json(silent=True) or {}
    days = max(20, min(500, int(data.get("days", 160))))
    seed = int(data.get("seed", 20260731))
    params = _get_model_user().params
    semantic_report = run_numerical_semantic_check(params)
    care_report = run_synthetic_care_frequency_check(
        params,
        days=days,
        seed=seed,
    )
    return jsonify(
        _json_safe(
            {
                "status": "success",
                "semantic_report": semantic_report,
                "care_frequency_report": care_report,
                "passed": bool(
                    semantic_report.get("passed") and care_report.get("passed")
                ),
                "note": "Engineering checks are not population or clinical validation.",
            }
        )
    )

@app.route('/api/simulate', methods=['POST'])
@auth_required
def simulate():
    """
    拉取或注入日程、过滤、织入例行、调用 Simulator.simulate_day；
    请求体可含 date、mock_events、shield_keywords、init_S/E、force_refresh 等。
    返回 JSON：图像 base64、告警、轨迹日志、事件画像等。
    """
    data = request.get_json(silent=True) or {}
    current_user = _get_model_user()
    authenticated_user_id = int(get_identity()["id"])
    profile_snapshot = application_database.latest_profile_snapshot(
        authenticated_user_id
    )
    _apply_profile_routine(current_user, profile_snapshot)
    date_str = str(data.get("date") or datetime.now().strftime("%Y-%m-%d"))
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
        mock_events = _validate_mock_events(data.get("mock_events", []))
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    
    god_params = {}
    if "K_resilience" in data: god_params["K_resilience"] = float(data["K_resilience"])
    if "fatigue_accel" in data: god_params["fatigue_acceleration"] = float(data["fatigue_accel"])
    if "Z_factor" in data: god_params["Z_factor"] = float(data["Z_factor"])
    if god_params:
        current_user.update_params(god_params)

    events_json = []
    
    force_refresh = data.get("force_refresh", False)
    
    try:
        from data_pipeline.fetcher import fetch_events_with_timeout

        feishu_api = FeishuAPI()
        token_info, _ = feishu_api.ensure_valid_token(
            _feishu_token_path(authenticated_user_id)
        )
        events_json = fetch_events_with_timeout(
            date_str=date_str,
            injected_token=token_info["access_token"],
            timeout=FEISHU_REQUEST_TIMEOUT_SECONDS,
            force_refresh=force_refresh,
            cache_namespace=f"user:{authenticated_user_id}",
        )
    except Exception as e:
        print(f"获取飞书日程失败，将回退到纯沙盒事件池: {e}")

    app_trace_logs = []

    shield_kws = data.get("shield_keywords", [])
    shield_time_ranges = data.get("shield_time_ranges", [])
    
    if (shield_kws or shield_time_ranges) and events_json:
        filtered_events_json = []
        for ev in events_json:
            name = ev.get("summary", ev.get("name", "")).strip()
            if any(kw == name for kw in shield_kws):
                app_trace_logs.append(f"[事件移除] 真实日程 '{name}' 命中名称屏蔽。")
                continue
                
            st_raw = ev.get("start_time", "")
            et_raw = ev.get("end_time", "")
            ev_start, ev_end = normalize_interval(st_raw, et_raw)
            
            is_time_blocked = False
            for tr in shield_time_ranges:
                s_limit = tr.get("start", "23:59")
                e_limit = tr.get("end", "00:00")
                if overlaps(ev_start, ev_end, s_limit, e_limit):
                    is_time_blocked = True
                    break
            
            if is_time_blocked:
                app_trace_logs.append(f"[时空移除] 真实日程 '{name}' ({ev_start}-{ev_end}) 命中时段屏蔽。")
                continue

            filtered_events_json.append(ev)
        events_json = filtered_events_json

    events = EventFactory.create_from_json(events_json)
    
    occupied_blocks = []
    for ev in events:
        try:
            st_str, et_str = normalize_interval(ev.start_time, ev.end_time)
            occupied_blocks.append((st_str, et_str, ev.name))
        except: pass

    for me in mock_events:
        st = f"{date_str} {me['start']}"
        et = f"{date_str} {me['end']}"
        raw_mock_metadata = me.get("metadata")
        mock_metadata = (
            dict(raw_mock_metadata)
            if isinstance(raw_mock_metadata, dict)
            else {}
        )
        for field in ("objective", "appraisal", "recovery"):
            if field in me:
                mock_metadata[field] = me[field]
        
        overlap = False
        for (obs, obe, ob_name) in occupied_blocks:
            if overlaps(me['start'], me['end'], obs, obe):
                overlap = True
                app_trace_logs.append(f"[时空防御] 沙盒注入事件 '{me['name']}' 与 '{ob_name}' 重叠，已拒绝。")
                break
        
        if overlap: continue
        occupied_blocks.append((me['start'], me['end'], me['name'])) 
        
        if me['type'] == 'course':
            from event.course_event import CourseEvent
            events.append(CourseEvent(
                f"mock_{me['name']}",
                st,
                et,
                name=me['name'],
                description=str(me.get("description") or ""),
                credit=float(me.get('credit', 2.0)),
                hours=float(me.get('hours', 32.0)),
                level=me.get("level"),
                metadata=mock_metadata,
            ))
        elif me['type'] == 'task':
            from event.task_event import TaskEvent
            events.append(TaskEvent(
                f"mock_{me['name']}",
                st,
                et,
                name=me['name'],
                description=str(me.get("description") or ""),
                task_type=me.get('level', 'general'),
                metadata=mock_metadata,
            ))
        elif me['type'] == 'rest':
            from event.rest_event import MealEvent, NapEvent, RestEvent
            subtype = me.get('subtype', 'rest')
            if subtype == 'meal': events.append(MealEvent(f"m_{me['name']}", st, et, name=me['name'], metadata=mock_metadata))
            elif subtype == 'nap': events.append(NapEvent(f"m_{me['name']}", st, et, name=me['name'], metadata=mock_metadata))
            else: events.append(RestEvent(f"m_{me['name']}", st, et, name=me['name'], metadata=mock_metadata))
        elif me['type'] == 'gym':
            from event.gym_event import GymEvent
            events.append(GymEvent(f"mock_{me['name']}", st, et, name=me['name'], intensity=float(me.get('intensity', 0.7)), metadata=mock_metadata))
        elif me['type'] == 'library':
            from event.library_event import LibraryEvent
            events.append(LibraryEvent(f"mock_{me['name']}", st, et, name=me['name'], study_intensity=float(me.get('study_intensity', 0.7)), metadata=mock_metadata))

    routine_plan_record = None
    context_record = None
    try:
        _, routine_plan_record, context_record = _get_or_create_routine_context(
            authenticated_user_id,
            date_str,
            events=events,
        )
        final_events = inject_routine_events(events, date_str, current_user)
    except Exception as e:
        print(f"注入生态日程失败: {e}")
        import traceback
        traceback.print_exc()
        app_trace_logs.append(f"生态日程织入异常，回退到原始事件: {e}")
        final_events = events
    
    previous_day_state = data.get("previous_day_state")
    if previous_day_state is not None and not isinstance(previous_day_state, dict):
        return jsonify({"status": "error", "message": "previous_day_state must be an object"}), 400
    cross_day_context = None
    auto_cross_day_context = bool(data.get("auto_cross_day_context", True))
    if not previous_day_state and auto_cross_day_context:
        try:
            cross_day_context = build_automatic_cross_day_context(
                application_database,
                authenticated_user_id,
                date_str,
                max_carry_days=int(
                    os.getenv("CROSS_DAY_UNFINISHED_MAX_DAYS", "3")
                ),
            )
        except (TypeError, ValueError) as exc:
            app_trace_logs.append(f"[跨日上下文] 无法构建，已回退到当日基线: {exc}")
        if cross_day_context:
            previous_day_state = dict(
                cross_day_context.get("previous_day_state") or {}
            )
            app_trace_logs.append(
                "[跨日上下文] 已接入前一日运行 "
                f"{cross_day_context.get('source_prediction_run_id')}；"
                f"明确未完成任务={len(cross_day_context.get('unfinished_tasks', []))}。"
            )
    previous_day_state = previous_day_state or {}
    if cross_day_context is None and previous_day_state:
        cross_day_context = {
            "schema_version": "cross_day_context.manual.v1",
            "target_date": date_str,
            "source_date": None,
            "source_prediction_run_id": None,
            "previous_day_state": dict(previous_day_state),
            "previous_day_end_stress_band": "manual",
            "unfinished_tasks": [],
            "unfinished_load": 0.0,
            "chain_depth": 1,
            "policy": {"source": "request_supplied"},
        }

    if context_record and context_record.get("context_snapshot_id"):
        updated_context = application_database.update_daily_context_previous_day(
            authenticated_user_id,
            context_record["context_snapshot_id"],
            cross_day_context,
        )
        if updated_context:
            context_record = updated_context

    event_semantic_context = semantic_context_from_cross_day(cross_day_context)
    if event_semantic_context:
        for event in final_events:
            if not isinstance(getattr(event, "metadata", None), dict):
                event.metadata = {}
            event.metadata.setdefault(
                "semantic_context",
                dict(event_semantic_context),
            )
    init_S = previous_day_state.get("S_end", data.get("init_S"))
    init_E = previous_day_state.get(
        "V_end", previous_day_state.get("E_end", data.get("init_E"))
    )
    init_P = previous_day_state.get("P_end", data.get("init_P"))
    init_F = previous_day_state.get("F_end", data.get("init_F"))
    if previous_day_state.get("sleep_debt") is not None:
        try:
            current_user.set_sleep_debt(float(previous_day_state["sleep_debt"]))
        except (TypeError, ValueError):
            return jsonify({"status": "error", "message": "sleep_debt must be numeric"}), 400
    
    if init_S is None or init_E is None:
        init_S = current_user.get_current_S_star()
        init_E = DEFAULT_INITIAL_ENERGY
    else:
        try:
            init_S, init_E = float(init_S), float(init_E)
        except (TypeError, ValueError):
            return jsonify(
                {"status": "error", "message": "init_S and init_E must be numbers"}
            ), 400
        if not (0 <= init_S <= 100 and 0 <= init_E <= 100):
            return jsonify(
                {"status": "error", "message": "init_S and init_E must be between 0 and 100"}
            ), 400
    try:
        init_P = None if init_P is None else float(init_P)
        init_F = None if init_F is None else float(init_F)
    except (TypeError, ValueError):
        return jsonify(
            {"status": "error", "message": "init_P and init_F must be numbers"}
        ), 400
    if (
        (init_P is not None and not 0 <= init_P <= 1)
        or (init_F is not None and not 0 <= init_F <= 1)
    ):
        return jsonify(
            {"status": "error", "message": "init_P and init_F must be between 0 and 1"}
        ), 400

    request_observations = data.get("observations", [])
    if not isinstance(request_observations, list):
        return jsonify({"status": "error", "message": "observations must be a list"}), 400
    stored_observations = _stored_ema_observations(
        authenticated_user_id,
        date_str,
    )
    all_observations = [*stored_observations, *request_observations]
    sleep_context = data.get("sleep_context") or {}
    if not isinstance(sleep_context, dict):
        return jsonify({"status": "error", "message": "sleep_context must be an object"}), 400
    try:
        sleep_quality_deviation = float(
            sleep_context.get("quality_deviation", 0.0) or 0.0
        )
    except (TypeError, ValueError):
        return jsonify(
            {"status": "error", "message": "sleep_context.quality_deviation must be numeric"}
        ), 400
    if not -1.0 <= sleep_quality_deviation <= 1.0:
        return jsonify(
            {"status": "error", "message": "sleep quality deviation must be within [-1, 1]"}
        ), 400

    use_cross_day_transition = bool(previous_day_state) or bool(
        data.get("cross_day_transition", False)
    )
    solver = current_user.solver
    result_tuple = solver.simulate_day(
        final_events,
        init_S,
        init_E,
        date_str,
        observations=all_observations,
        prev_P_end=init_P,
        prev_F_end=init_F,
        cross_day_transition=use_cross_day_transition,
        sleep_quality_deviation=sleep_quality_deviation,
        cross_day_context=cross_day_context,
    )
    
    results, end_S, end_E, _, _, alerts, confidence_series, solver_logs, profile_list, wake_s = result_tuple
    event_trajectory = [
        profile["trajectory"]
        for profile in profile_list
        if profile.get("trajectory") is not None
    ]
    semantic_snapshot = []
    semantic_run_info = []
    for profile in profile_list:
        semantic = (profile.get("assessment") or {}).get("semantic") or {}
        if not semantic:
            continue
        semantic_snapshot.append(
            {
                "name": profile.get("name"),
                "time": profile.get("time"),
                "fingerprint": semantic.get("fingerprint"),
                "values": semantic.get("values"),
                "rule_values": semantic.get("rule_values"),
                "external_values": semantic.get("external_values"),
                "rule_version": semantic.get("rule_version"),
                "prompt_version": semantic.get("prompt_version"),
                "fusion_policy_version": semantic.get("fusion_policy_version"),
                "provider": semantic.get("provider"),
                "model": semantic.get("model"),
                "prompt_sha256": semantic.get("prompt_sha256"),
                "evidence_tags": semantic.get("evidence_tags", []),
                "reasoning_summary": semantic.get("reasoning_summary", ""),
            }
        )
        semantic_run_info.append(
            {
                "name": profile.get("name"),
                "time": profile.get("time"),
                "source": semantic.get("source"),
                "cache_hit": bool(semantic.get("cache_hit")),
                "external_error": semantic.get("external_error"),
                "matched_rules": semantic.get("matched_rules", []),
                "constraints_applied": semantic.get("constraints_applied", []),
                "evidence_tags": semantic.get("evidence_tags", []),
                "reasoning_summary": semantic.get("reasoning_summary", ""),
            }
        )
    semantic_snapshot.sort(
        key=lambda item: (
            str(item.get("time") or ""),
            str(item.get("name") or ""),
            str(item.get("fingerprint") or ""),
        )
    )
    
    final_logs = app_trace_logs + solver_logs
    
    # Phase 0 simulations are replayable reads. They never evolve or persist
    # user baselines; later feedback/calibration flows create explicit versions.
    new_S_star = current_user.get_current_S_star()
    new_threshold = current_user.get_current_threshold()
    

    img_base64 = get_plot_image_base64(
        results, confidence_series, alerts, 
        params=current_user.params, 
        S_star=new_S_star,
        events=final_events 
    )
    
    chart_markdown = f"![今日压力流转图](data:image/png;base64,{img_base64})" if img_base64 else ""

    prediction_run_id = new_id()
    seed = int(current_user.get_param("random_seed", 42))
    run_input = {
        "date": date_str,
        "events": events_json,
        "mock_events": mock_events,
        "shield_keywords": shield_kws,
        "shield_time_ranges": shield_time_ranges,
        "init_S": init_S,
        "init_E": init_E,
        "init_P": init_P,
        "init_F": init_F,
        "observations": all_observations,
        "stored_observation_count": len(stored_observations),
        "sleep_context": sleep_context,
        "cross_day_transition": use_cross_day_transition,
        "auto_cross_day_context": auto_cross_day_context,
        "previous_day_state": previous_day_state,
        "cross_day_context": cross_day_context,
        "random_seed": seed,
        "profile_snapshot_id": (
            profile_snapshot.get("profile_snapshot_id")
            if profile_snapshot
            else None
        ),
        "routine_plan_id": (
            routine_plan_record.get("routine_plan_id")
            if routine_plan_record
            else None
        ),
        # This frozen semantic decision set is part of the replay contract.
        # Operational fields such as cache_hit are deliberately excluded.
        "semantic_snapshot": semantic_snapshot,
    }
    result_summary = {
        "end_S": round(float(end_S), 4),
        "end_E": round(float(end_E), 4),
        "end_V": round(float(end_E), 4),
        "end_P": round(float(results[-1].get("P", 0.0)), 4) if results else 0.0,
        "end_F": round(float(results[-1].get("F", 0.0)), 4) if results else 0.0,
        "alerts": alerts,
        "point_count": len(results),
        "baseline_S": new_S_star,
        "stress_threshold": new_threshold,
        "model_variant": results[-1].get("model_variant") if results else None,
        "active_states": results[-1].get("active_states", ["S"]) if results else ["S"],
        "trajectory_warning_count": sum(
            item.get("status") == "warning" for item in event_trajectory
        ),
    }
    fingerprint_payload = json.dumps(
        {"input": run_input, "result": result_summary},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    input_fingerprint = hashlib.sha256(fingerprint_payload).hexdigest()
    application_database.save_prediction_run(
        authenticated_user_id,
        {
            "prediction_run_id": prediction_run_id,
            "context_snapshot_id": (
                context_record.get("context_snapshot_id")
                if context_record
                else None
            ),
            "local_date": date_str,
            "schema_version": "prediction_run.v1",
            "model_version": MODEL_VERSION,
            "parameter_version": PARAMETER_VERSION,
            "feature_version": FEATURE_VERSION,
            "random_seed": seed,
            "input": run_input,
            "result": {**result_summary, "fingerprint": input_fingerprint},
            "created_at": utc_now(),
            "diagnostics": {
                "schema_version": "prediction_diagnostics.v2",
                "event_profiles": profile_list,
                "event_trajectory": event_trajectory,
                "semantic_inference": semantic_run_info,
                "trace_logs": final_logs,
            },
        },
        results,
    )
    
    return jsonify({
        "status": "success",
        "prediction_run_id": prediction_run_id,
        "input_fingerprint": input_fingerprint,
        "versions": {
            "model": MODEL_VERSION,
            "parameters": PARAMETER_VERSION,
            "features": FEATURE_VERSION,
        },
        "image": img_base64,
        "chart_markdown": chart_markdown,
        "end_S": end_S,
        "end_E": end_E,
        "end_V": end_E,
        "end_P": results[-1].get("P", 0.0) if results else 0.0,
        "end_F": results[-1].get("F", 0.0) if results else 0.0,
        "model_variant": results[-1].get("model_variant") if results else None,
        "active_states": results[-1].get("active_states", ["S"]) if results else ["S"],
        "stored_observation_count": len(stored_observations),
        "new_S_star": new_S_star,
        "new_threshold": new_threshold,
        "alerts": alerts,
        "trace_logs": final_logs,
        "event_profile": profile_list,
        "event_trajectory": event_trajectory,
        "semantic_inference": {
            "schema_version": "semantic_run_info.v1",
            "replay_policy": "stored_trajectory_or_frozen_semantic_cache_then_rules",
            "agent": semantic_agent_status(),
            "items": semantic_run_info,
        },
        "cross_day_context": cross_day_context,
        "routine_plan": routine_plan_record,
        "used_init_S": init_S, 
        "used_init_E": init_E
    })
      
@app.route('/api/token_status', methods=['GET'])
@auth_required
def token_status():
    """Check and, when possible, automatically refresh the current user's token."""
    try:
        api = FeishuAPI(require_secret=False)
        result = api.get_connection_status(
            _feishu_token_path(int(get_identity()["id"])),
            refresh=bool(api.app_secret),
        )
        result["configured"] = bool(api.app_id and api.app_secret)
        result["oauth_app_id"] = api.app_id
        result["redirect_uri"] = api.redirect_uri
        return jsonify(result)
    except Exception:
        return jsonify({
            "valid": False,
            "connected": False,
            "status": "configuration_error",
            "configured": False,
            "needs_reauthorization": False,
            "refreshable": False,
        })


@app.route('/api/feishu/verify', methods=['GET'])
@session_required
def feishu_verify():
    """Verify that the saved token can read the current user's primary calendar."""
    try:
        from utils.get_calendar_id import CalendarIDFetcher

        api = FeishuAPI()
        token_info, token_state = api.ensure_valid_token(
            _feishu_token_path(int(get_identity()["id"]))
        )
        calendar = CalendarIDFetcher().get_calendar_info(
            token_info["access_token"]
        )
        if not calendar.get("calendar_id"):
            raise ValueError("飞书未返回可访问的主日历")
        return jsonify({
            "status": "success",
            "valid": True,
            "token_state": token_state,
            "verified_at": utc_now(),
            "calendar": {
                "summary": calendar.get("summary") or "我的主日历",
                "role": calendar.get("role"),
                "type": calendar.get("type") or "primary",
            },
        })
    except Exception as exc:
        return jsonify({
            "status": "error",
            "valid": False,
            "message": f"连接检测失败：{str(exc)}",
        }), 400


if __name__ == '__main__':
    print("压力建模沙盒 Web 服务 已启动...")
    print(f"访问地址: http://localhost:{APP_DEFAULT_PORT}")
    app.run(debug=True, port=APP_DEFAULT_PORT)
