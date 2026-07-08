import sys
import os

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '../'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from flask import Flask, render_template, request, jsonify
import os
import json
from datetime import datetime, timedelta
from entity.user import User
from utils.event_factory import EventFactory
from utils.get_token import FeishuAPI, get_user_access_token 
from visualization.plotter import get_plot_image_base64
from data_pipeline.orchestrator import inject_routine_events, process_date
from algorithm.time_utils import normalize_interval, overlaps
from calibration.calibrator import calibrate_parameters
from calibration.metrics import evaluate_simulation
from calibration.parameter_validation import validate_params
from calibration.simulation_runner import run_simulation_for_calibration
from calibration.storage import CalibrationStore
from settings.model_defaults import (
    APP_DEFAULT_PORT,
    DEFAULT_INITIAL_ENERGY,
    DEFAULT_USER_ID,
    FEISHU_REQUEST_TIMEOUT_SECONDS,
)

import lark_oapi as lark

template_dir = os.path.join(project_root, 'templates')
static_dir = os.path.join(project_root, 'static')

app = Flask(__name__, template_folder=template_dir, static_folder=static_dir)
current_user = User(user_id=DEFAULT_USER_ID)


def _json_safe(value):
    """Convert tuple-keyed config dictionaries into JSON-safe payloads."""
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value

@app.route('/')
def index():
    """Web 首页。"""
    return render_template('index.html')

@app.route('/api/feishu/get_url', methods=['GET'])
def feishu_get_url():
    """返回飞书 OAuth 授权跳转 URL（JSON: url）。"""
    try:
        api = FeishuAPI() 
        url = api.generate_authorize_url()
        return jsonify({"status": "success", "url": url})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

@app.route('/api/feishu/submit_code', methods=['POST'])
def feishu_submit_code():
    """POST JSON: code，换 user_access_token 并落盘。"""
    code = request.json.get('code', '').strip()
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
def handle_config():
    """GET 返回当前用户参数；POST 更新策略与 params（内存）。"""
    global current_user
    if request.method == 'GET':
        safe_params = User._params_to_json_safe(current_user.params)
        return jsonify({"user_id": current_user.user_id, "params": safe_params})
    elif request.method == 'POST':
        data = request.json
        if data.get("user_id") and data.get("user_id") != current_user.user_id:
            current_user = User(user_id=data.get("user_id"))
        
        new_params = data.get("params", {})
        current_user.update_strategy_config(
            f_strategy=new_params.get("f_strategy"),
            C_strategy=new_params.get("C_strategy"),
            night_strategy=new_params.get("night_strategy"),
            rest_strategy=new_params.get("rest_strategy"),
            time_preferences=new_params.get("time_preferences", [])
        )
        current_user.update_params(new_params)
        current_user.save_config()
        return jsonify({"status": "success", "message": "配置已更新(仅内存生效)"})

@app.route('/api/params/validate', methods=['POST'])
def validate_runtime_params():
    """Validate a params payload before simulation or calibration."""
    data = request.json or {}
    params = data.get("params", current_user.params)
    return jsonify({"status": "success", "validation": validate_params(params)})

@app.route('/api/feedback/daily', methods=['POST'])
def save_daily_feedback():
    """Store one day's lightweight self-report feedback in SQLite."""
    data = request.json or {}
    if not data.get("date"):
        return jsonify({"status": "error", "message": "date is required"}), 400
    row_id = CalibrationStore().record_daily_feedback(data, user_id=data.get("user_id", DEFAULT_USER_ID))
    return jsonify({"status": "success", "id": row_id})

@app.route('/api/feedback/event', methods=['POST'])
def save_event_feedback():
    """Store event-level correction feedback for classification and intensity."""
    data = request.json or {}
    if not data.get("date"):
        return jsonify({"status": "error", "message": "date is required"}), 400
    row_id = CalibrationStore().record_event_feedback(data, user_id=data.get("user_id", DEFAULT_USER_ID))
    return jsonify({"status": "success", "id": row_id})

@app.route('/api/evaluate', methods=['POST'])
def evaluate_curve():
    """Evaluate simulated or supplied curve results against feedback anchors."""
    data = request.json or {}
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
        store.record_evaluation(metrics, user_id=data.get("user_id", DEFAULT_USER_ID), notes=data.get("notes"))
        if simulation is not None:
            store.record_model_run(
                date=data.get("date"),
                results=simulation["results"],
                final_state=simulation["final_state"],
                alerts=simulation["alerts"],
                user_id=data.get("user_id", DEFAULT_USER_ID),
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
def calibrate_curve_params():
    """Run lightweight local parameter calibration against feedback samples."""
    data = request.json or {}
    samples = data.get("samples", [])
    if not samples:
        return jsonify({"status": "error", "message": "samples is required"}), 400
    iterations = max(1, min(300, int(data.get("iterations", 60))))
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
        version_name = data.get("version_name", f"calibrated_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
        store.record_parameter_version(
            report["best_params"],
            version_name=version_name,
            user_id=data.get("user_id", DEFAULT_USER_ID),
            parent_version=data.get("base_params_version"),
            notes=data.get("notes"),
        )
        store.record_calibration_job(
            report,
            user_id=data.get("user_id", DEFAULT_USER_ID),
            base_params_version=data.get("base_params_version"),
            best_params_version=version_name,
        )

    return jsonify(_json_safe({"status": "success", "report": report}))

@app.route('/api/simulate', methods=['POST'])
def simulate():
    """
    拉取或注入日程、过滤、织入例行、调用 Simulator.simulate_day；
    请求体可含 date、mock_events、shield_keywords、init_S/E、force_refresh 等。
    返回 JSON：图像 base64、告警、轨迹日志、事件画像等。
    """
    data = request.json
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
