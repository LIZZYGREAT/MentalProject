import sys
import os

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '../'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from flask import Flask, g, render_template, request, jsonify, session
import hmac
import secrets
from datetime import datetime, timedelta
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
from calibration.parameter_validation import validate_params
from calibration.simulation_runner import run_simulation_for_calibration
from calibration.storage import CalibrationStore
from settings.model_defaults import (
    APP_DEFAULT_PORT,
    DEFAULT_CALLBACK_PATH,
    DEFAULT_INITIAL_ENERGY,
    FEISHU_REQUEST_TIMEOUT_SECONDS,
)

template_dir = os.path.join(project_root, 'templates')
static_dir = os.path.join(project_root, 'static')

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

bootstrap_username = os.getenv("BOOTSTRAP_ADMIN_USERNAME")
bootstrap_password = os.getenv("BOOTSTRAP_ADMIN_PASSWORD")
if bootstrap_username and bootstrap_password:
    if application_database.get_user_by_username(bootstrap_username) is None:
        application_database.create_user(
            bootstrap_username,
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


@app.route('/')
@auth_required
def index():
    """Web 首页。"""
    return render_template('index.html', auth_user=get_identity())


@app.route('/login', methods=['GET'])
def login_page():
    """Browser login page."""
    if get_identity():
        return render_template('index.html', auth_user=get_identity())
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
    username = str(data.get("username") or "")
    password = str(data.get("password") or "")
    user = application_database.authenticate_password(
        username,
        password,
    )
    if not user:
        application_database.record_audit(
            "auth.login_failed",
            ip_address=request.remote_addr,
            details={"username": username[:64]},
        )
        return jsonify(
            {
                "status": "error",
                "code": "invalid_credentials",
                "message": "用户名或密码错误",
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
            data.get("username", ""),
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

@app.route('/api/feishu/get_url', methods=['GET'])
@session_required
def feishu_get_url():
    """返回飞书 OAuth 授权跳转 URL（JSON: url）。"""
    try:
        api = FeishuAPI(require_secret=False)
        oauth_state = secrets.token_urlsafe(24)
        session["feishu_oauth_state"] = oauth_state
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
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400

@app.route(DEFAULT_CALLBACK_PATH, methods=['GET'])
def feishu_callback():
    """飞书 OAuth 回调：自动用 code 换 token，并保存到 data/user_token.json。"""
    code = request.args.get("code", "").strip()
    error = request.args.get("error", "").strip()
    error_description = request.args.get("error_description", "").strip()
    expected_state = session.pop("feishu_oauth_state", "")
    received_state = request.args.get("state", "").strip()
    if (
        not expected_state
        or not received_state
        or not hmac.compare_digest(expected_state, received_state)
    ):
        return "<h3>飞书授权失败</h3><p>OAuth state 校验失败，请重新发起授权。</p>", 400
    if error:
        return (
            f"<h3>飞书授权失败</h3><p>{error}</p><p>{error_description}</p>"
            "<p>可以关闭此页面回到本地调试台。</p>",
            400,
        )
    if not code:
        return "<h3>飞书回调缺少 code</h3><p>请重新点击授权按钮。</p>", 400

    try:
        api = FeishuAPI()
        token_info = api.get_user_access_token(code)
        api.save_token_to_file(token_info)
        return (
            "<h3>飞书授权成功</h3>"
            "<p>Token 已保存到本地 data/user_token.json。</p>"
            "<p>现在可以关闭此页面，回到调试台刷新 Token 状态。</p>"
        )
    except Exception as e:
        return (
            f"<h3>飞书 Token 换取失败</h3><p>{str(e)}</p>"
            "<p>请确认飞书后台配置的重定向 URL 与本项目 REDIRECT_URI 完全一致。</p>",
            500,
        )

@app.route('/api/feishu/submit_code', methods=['POST'])
@session_required
def feishu_submit_code():
    """POST JSON: code，换 user_access_token 并落盘。"""
    payload = request.json or {}
    code = payload.get('code', '').strip()
    if not code:
        return jsonify({"status": "error", "message": "请输入授权码"})
    
    try:
        api = FeishuAPI()
        token_info = api.get_user_access_token(code)
        
        if token_info:
            api.save_token_to_file(token_info)
            return jsonify({"status": "success", "message": "Token 获取成功并已保存！"})
        else:
            return jsonify({"status": "error", "message": "Code 无效或已过期，请重新获取"})
    except Exception as e:
        return jsonify({"status": "error", "message": f"处理异常: {str(e)}"})

@app.route('/api/config', methods=['GET', 'POST'])
@auth_required
def handle_config():
    """GET 返回当前用户参数；POST 更新策略与 params（内存）。"""
    current_user = _get_model_user()
    if request.method == 'GET':
        safe_params = User._params_to_json_safe(current_user.params)
        return jsonify({"user_id": current_user.user_id, "params": safe_params})
    elif request.method == 'POST':
        data = request.get_json(silent=True) or {}
        new_params = data.get("params", {})
        if not isinstance(new_params, dict):
            return jsonify({"status": "error", "message": "params must be an object"}), 400
        validation = validate_params({**current_user.params, **new_params})
        if not validation["valid"]:
            return jsonify(
                {
                    "status": "error",
                    "message": "参数校验失败",
                    "validation": validation,
                }
            ), 400
        current_user.update_strategy_config(
            f_strategy=new_params.get("f_strategy"),
            C_strategy=new_params.get("C_strategy"),
            night_strategy=new_params.get("night_strategy"),
            rest_strategy=new_params.get("rest_strategy"),
            time_preferences=new_params.get("time_preferences", [])
        )
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
    date_str = data.get("date", datetime.now().strftime("%Y-%m-%d"))
    
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
        try:
            from entry.feishu_config import FEISHU_CALENDAR_ID
        except ImportError:
            from feishu_config import FEISHU_CALENDAR_ID
        
        events_json = fetch_events_with_timeout(
            date_str=date_str, 
            injected_calendar_id=FEISHU_CALENDAR_ID,
            timeout=FEISHU_REQUEST_TIMEOUT_SECONDS,
            force_refresh=force_refresh
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
    
    mock_events = data.get("mock_events", [])
    occupied_blocks = []
    for ev in events:
        try:
            st_str, et_str = normalize_interval(ev.start_time, ev.end_time)
            occupied_blocks.append((st_str, et_str, ev.name))
        except: pass

    for me in mock_events:
        st = f"{date_str} {me['start']}"
        et = f"{date_str} {me['end']}"
        
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
            events.append(CourseEvent(f"mock_{me['name']}", st, et, name=me['name'], credit=float(me.get('credit', 2.0)), hours=float(me.get('hours', 32.0))))
        elif me['type'] == 'task':
            from event.task_event import TaskEvent
            events.append(TaskEvent(f"mock_{me['name']}", st, et, name=me['name'], task_type=me.get('level', 'general')))
        elif me['type'] == 'rest':
            from event.rest_event import MealEvent, NapEvent, RestEvent
            subtype = me.get('subtype', 'rest')
            if subtype == 'meal': events.append(MealEvent(f"m_{me['name']}", st, et, name=me['name']))
            elif subtype == 'nap': events.append(NapEvent(f"m_{me['name']}", st, et, name=me['name']))
            else: events.append(RestEvent(f"m_{me['name']}", st, et, name=me['name']))
        elif me['type'] == 'gym':
            from event.gym_event import GymEvent
            events.append(GymEvent(f"mock_{me['name']}", st, et, name=me['name'], intensity=float(me.get('intensity', 0.7))))
        elif me['type'] == 'library':
            from event.library_event import LibraryEvent
            events.append(LibraryEvent(f"mock_{me['name']}", st, et, name=me['name'], study_intensity=float(me.get('study_intensity', 0.7))))

    try:
        final_events = inject_routine_events(events, date_str, current_user)
    except Exception as e:
        print(f"注入生态日程失败: {e}")
        import traceback
        traceback.print_exc()
        app_trace_logs.append(f"生态日程织入异常，回退到原始事件: {e}")
        final_events = events
    
    init_S = data.get("init_S")
    init_E = data.get("init_E")
    
    if init_S is None or init_E is None:
        init_S = current_user.get_current_S_star()
        init_E = DEFAULT_INITIAL_ENERGY
    else:
        init_S, init_E = float(init_S), float(init_E)

    solver = current_user.solver
    result_tuple = solver.simulate_day(final_events, init_S, init_E, date_str)
    
    results, end_S, end_E, _, _, alerts, confidence_series, solver_logs, profile_list, wake_s = result_tuple
    
    final_logs = app_trace_logs + solver_logs
    
    if results:
        daily_mean_s = sum(r["S"] for r in results) / len(results)
        has_red_alert = any("红" in a.get("type", "") or "严重" in a.get("type", "") for a in alerts)
        current_user.evolve_daily_baseline(wake_s, daily_mean_s, has_red_alert)
        _save_model_user(current_user)
        
    new_S_star = current_user.get_current_S_star()
    new_threshold = current_user.get_current_threshold()
    

    img_base64 = get_plot_image_base64(
        results, confidence_series, alerts, 
        params=current_user.params, 
        S_star=new_S_star,
        events=final_events 
    )
    
    chart_markdown = f"![今日压力流转图](data:image/png;base64,{img_base64})" if img_base64 else ""
    
    return jsonify({
        "status": "success",
        "image": img_base64,
        "chart_markdown": chart_markdown,
        "end_S": end_S,
        "end_E": end_E,
        "new_S_star": new_S_star,
        "new_threshold": new_threshold,
        "alerts": alerts,
        "trace_logs": final_logs,
        "event_profile": profile_list,
        "used_init_S": init_S, 
        "used_init_E": init_E
    })
      
@app.route('/api/token_status', methods=['GET'])
@auth_required
def token_status():
    """检查本地飞书 token 是否存在且未过期。"""
    try:
        api = FeishuAPI()
        token = api.load_token_from_file()
        valid = False
        if token and not api.is_token_expired(token):
            valid = True
        return jsonify({"valid": valid})
    except:
        return jsonify({"valid": False})

if __name__ == '__main__':
    print("压力建模沙盒 Web 服务 已启动...")
    print(f"访问地址: http://localhost:{APP_DEFAULT_PORT}")
    app.run(debug=True, port=APP_DEFAULT_PORT)
