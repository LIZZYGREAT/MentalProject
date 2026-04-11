# data_pipeline/orchestrator.py
from datetime import datetime, timedelta
from entity.user import User
from utils.event_factory import EventFactory
from utils.routine_weaver import RoutineWeaver
from data_pipeline.fetcher import fetch_events_with_timeout
from visualization.plotter import get_plot_image_base64  

def inject_routine_events(base_events, date_str, user):
    """委托 RoutineWeaver 织入睡眠/三餐/午睡等例行事件。"""
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
    """拉取或注入日程、织入例行、用 User 推演一日并可选出图。"""
    print(f"开始推演日期: {date_str}")

    # 1. 初始化用户
    user_params = injected_user_profile if injected_user_profile else {}
    user = User(user_id="default", params=user_params, load_from_file=False)

    # 2. 获取日前日程
    events_json = None
    if injected_events is not None:
        print("接收到注入的事件，跳过网络/本地拉取。")
        events_json = injected_events
    else:
        print(f"尝试获取日程 (force_refresh={force_refresh})...")
        events_json = fetch_events_with_timeout(
            date_str, open_id, injected_token, injected_calendar_id, timeout=5.0, force_refresh=force_refresh
        )
            
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
        print("未注入昨日状态，采用当前 S* 与满精力启动。")
        prev_S = user.get_current_S_star()
        prev_E = 100.0

    # 5. 执行核心推演
    solver = user.solver
    result_tuple = solver.simulate_day(final_events, prev_S, prev_E, date_str)
    results, end_S, end_E, t_wake, t_sleeps, alerts, confidence, logs, profiles, wake_s = result_tuple
    
    # 6. 计算生态演进与双轨结算
    if results:
        daily_mean_s = sum(r["S"] for r in results) / len(results)
        has_red_alert = any("红" in a.get("type", "") or "严重" in a.get("type", "") for a in alerts)
        user.evolve_daily_baseline(wake_s, daily_mean_s, has_red_alert)
        

    # 7. 调用隔离后的画图引擎
    img_base64 = None
    if results:
        img_base64 = get_plot_image_base64(results, confidence, alerts, params=user.params, S_star=user.get_current_S_star(), events=final_events)

    return {
        "status": "success", "date": date_str,
        "final_state": {"S_end": end_S, "E_end": end_E, "S_star": user.get_current_S_star(), "S_threshold": user.get_current_threshold(), "sleep_debt": user.get_sleep_debt()},
        "alerts": alerts, "plot_base64": img_base64, "logs": logs
    }