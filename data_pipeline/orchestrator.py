# data_pipeline/orchestrator.py
from datetime import datetime, timedelta
from entity.user import User
from utils.event_factory import EventFactory
from utils.routine_weaver import RoutineWeaver

from data_pipeline.local_cache import load_calendar_events, load_stress_records, save_stress_records
from data_pipeline.fetcher import fetch_events_with_timeout
from visualization.plotter import get_plot_image_base64  

def inject_routine_events(base_events, date_str, user):
    weaver = RoutineWeaver(user)
    if hasattr(weaver, 'weave'):
        return weaver.weave(base_events, date_str)
    elif hasattr(weaver, 'inject_routine_events'):
        return weaver.inject_routine_events(base_events, date_str)
    return base_events

def process_date(
    date_str: str, 
    injected_events: list = None,
    injected_token: str = None,
    injected_calendar_id: str = None,
    injected_user_profile: dict = None,
    injected_yesterday_state: dict = None,
    force_refresh: bool = False, 
    open_id: str = None
) -> dict:
    """标准编排管道入口"""
    print(f"\n{'='*50}\n🚀 开始推演日期: {date_str} (云端Agent模式)\n{'='*50}")

    # 1. 初始化用户
    user_params = injected_user_profile if injected_user_profile else {}
    disable_io = bool(injected_events is not None or injected_yesterday_state is not None)
    user = User(user_id="default", params=user_params, load_from_file=not disable_io)

    # 2. 获取日前日程
    events_json = None
    if injected_events is not None:
        print("✅ 接收到 Agent 注入的事件，跳过所有网络/本地拉取。")
        events_json = injected_events
    else:
        if not force_refresh:
            events_json = load_calendar_events(date_str)
            if events_json is not None:
                print("✅ 找到本地缓存的日程，直接使用。")
        
        if events_json is None:
            print("🌐 触发兜底：从飞书接口拉取日程...")
            events_json = fetch_events_with_timeout(date_str, open_id, injected_token, injected_calendar_id, timeout=5.0)
            
    if not events_json:
        events_json = []

    # 3. 生成领域事件与日常插入
    base_events = EventFactory.create_from_json(events_json)
    final_events = inject_routine_events(base_events, date_str, user)

    # 4. 初始化昨日状态
    prev_S, prev_E = None, None
    if injected_yesterday_state:
        prev_S = injected_yesterday_state.get("S_end")
        prev_E = injected_yesterday_state.get("E_end")
        user.set_stress_baseline(injected_yesterday_state.get("S_star", 50.0), injected_yesterday_state.get("S_threshold", 90.0))
        user.set_sleep_debt(injected_yesterday_state.get("sleep_debt", 0.0))
    else:
        records = load_stress_records()
        prev_date = (datetime.strptime(date_str, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
        if prev_date in records:
            prev_S = records[prev_date].get("end_stress")
            prev_E = records[prev_date].get("end_energy")

    # 5. 执行核心推演 (暂时还调用旧的 solver)
    solver = user.solver
    result_tuple = solver.simulate_day(final_events, prev_S, prev_E, date_str)
    results, end_S, end_E, t_wake, t_sleeps, alerts, confidence, logs, profiles, wake_s = result_tuple
    
    # 6. 计算生态演进与双轨结算
    if results:
        daily_mean_s = sum(r["S"] for r in results) / len(results)
        has_red_alert = any("红" in a.get("type", "") or "严重" in a.get("type", "") for a in alerts)
        user.evolve_daily_baseline(wake_s, daily_mean_s, has_red_alert)
        
    if not disable_io:
        records = load_stress_records()
        records[date_str] = {
            "end_stress": end_S, "end_energy": end_E,
            "S_star": user.get_current_S_star(),
            "S_threshold": user.get_current_threshold(),
            "wake_time": t_wake.strftime("%H:%M") if hasattr(t_wake, 'strftime') else "07:30",
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        save_stress_records(records)

    # 7. 调用隔离后的画图引擎
    img_base64 = None
    if results:
        img_base64 = get_plot_image_base64(results, confidence, alerts, params=user.params, S_star=user.get_current_S_star(), events=final_events)

    return {
        "status": "success", "date": date_str,
        "final_state": {"S_end": end_S, "E_end": end_E, "S_star": user.get_current_S_star(), "S_threshold": user.get_current_threshold(), "sleep_debt": user.get_sleep_debt()},
        "alerts": alerts, "plot_base64": img_base64, "logs": logs
    }