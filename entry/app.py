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

import lark_oapi as lark

template_dir = os.path.join(project_root, 'templates')
static_dir = os.path.join(project_root, 'static')

app = Flask(__name__, template_folder=template_dir, static_folder=static_dir)
current_user = User(user_id="default")

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/feishu/get_url', methods=['GET'])
def feishu_get_url():
    try:
        api = FeishuAPI() 
        url = api.generate_authorize_url()
        return jsonify({"status": "success", "url": url})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

@app.route('/api/feishu/submit_code', methods=['POST'])
def feishu_submit_code():
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

@app.route('/api/simulate', methods=['POST'])
def simulate():
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
        from feishu_config import FEISHU_CALENDAR_ID
        
        events_json = fetch_events_with_timeout(
            date_str=date_str, 
            injected_calendar_id=FEISHU_CALENDAR_ID,
            timeout=5.0,
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
                app_trace_logs.append(f"🗑️ [事件移除] 真实日程 '{name}' 命中名称屏蔽。")
                continue
                
            st_raw = ev.get("start_time", "")
            et_raw = ev.get("end_time", "")
            ev_start = st_raw.split(' ')[-1][:5] if st_raw else "00:00"
            ev_end = et_raw.split(' ')[-1][:5] if et_raw else "00:00"
            
            is_time_blocked = False
            for tr in shield_time_ranges:
                s_limit = tr.get("start", "23:59")
                e_limit = tr.get("end", "00:00")
                if max(ev_start, s_limit) < min(ev_end, e_limit):
                    is_time_blocked = True
                    break
            
            if is_time_blocked:
                app_trace_logs.append(f"🗑️ [时空移除] 真实日程 '{name}' ({ev_start}-{ev_end}) 命中时段屏蔽。")
                continue

            filtered_events_json.append(ev)
        events_json = filtered_events_json

    events = EventFactory.create_from_json(events_json)
    
    mock_events = data.get("mock_events", [])
    occupied_blocks = []
    for ev in events:
        try:
            st_str = ev.start_time.split(' ')[-1][:5] if isinstance(ev.start_time, str) else ev.start_time.strftime("%H:%M")
            et_str = ev.end_time.split(' ')[-1][:5] if isinstance(ev.end_time, str) else ev.end_time.strftime("%H:%M")
            occupied_blocks.append((st_str, et_str, ev.name))
        except: pass

    for me in mock_events:
        st = f"{date_str} {me['start']}"
        et = f"{date_str} {me['end']}"
        
        overlap = False
        for (obs, obe, ob_name) in occupied_blocks:
            if me['start'] < obe and me['end'] > obs:
                overlap = True
                app_trace_logs.append(f"❌ [时空防御] 沙盒注入事件 '{me['name']}' 与 '{ob_name}' 重叠，已拒绝！")
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
        print(f"❌ 注入生态日程失败: {e}")
        import traceback
        traceback.print_exc()
        app_trace_logs.append(f"❌ 生态日程织入异常，回退到原始事件: {e}")
        final_events = events
    
    init_S = data.get("init_S")
    init_E = data.get("init_E")
    
    # 如果前端没有显式传递 S 和 E，强制回归健康基准线
    if init_S is None or init_E is None:
        init_S = current_user.get_current_S_star()
        init_E = 100.0
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
    print("访问地址: http://localhost:5000")
    app.run(debug=True, port=5000)