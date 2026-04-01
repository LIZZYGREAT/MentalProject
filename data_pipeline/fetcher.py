# data_pipeline/fetcher.py
import concurrent.futures
import time

# ==========================================
# 轻量级 TTL (Time-To-Live) 内存缓存
# 结构: { "YYYY-MM-DD": {"timestamp": float, "events": list} }
# ==========================================
_TTL_CACHE = {}
CACHE_EXPIRY_SECONDS = 300  # 缓存存活时间：5分钟

def fetch_events_from_calendar_internal(date_str, open_id=None, injected_token=None, injected_calendar_id=None):
    from utils.get_token import get_user_access_token
    from utils.calendar_tool import get_events_in_date_range
    import lark_oapi as lark
    
    access_token = injected_token
    if not access_token:
        token_info = get_user_access_token(interactive=False)
        if not token_info:
            print("❌ 无法获取用户 Token, 请先运行 get_token.py 授权")
            return []
        access_token = token_info["access_token"]
        
    calendar_id = injected_calendar_id
    if not calendar_id:
        from utils.get_calendar_id import CalendarIDFetcher
        fetcher = CalendarIDFetcher()
        calendar_id = fetcher.get_calendar_id(open_id)
        if not calendar_id:
            print("❌ 无法获取主日历 ID")
            return []
            
    client = lark.Client.builder().enable_set_token(True).build()
    events = get_events_in_date_range(client, access_token, calendar_id, date_str, date_str)
    
    return events

def fetch_events_with_timeout(date_str, open_id=None, injected_token=None, injected_calendar_id=None, timeout=5.0, force_refresh=False):
    global _TTL_CACHE
    
    current_time = time.time()
    
    # 1. 拦截层：检查 TTL 缓存 (附带 force_refresh 主动击穿)
    if not force_refresh and date_str in _TTL_CACHE:
        cached_data = _TTL_CACHE[date_str]
        age = current_time - cached_data["timestamp"]
        if age < CACHE_EXPIRY_SECONDS:
            print(f"⚡ [TTL缓存命中] 获取 {date_str} 的日程，拦截多余网络请求 (寿命剩余: {int(CACHE_EXPIRY_SECONDS - age)}s)")
            return cached_data["events"]

    if force_refresh:
        print(f"🔄 [强制刷新] 主动击穿 TTL 缓存，准备发起物理网络请求...")

    # 2. 物理请求层
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            fetch_events_from_calendar_internal, 
            date_str, open_id, injected_token, injected_calendar_id
        )
        try:
            events = future.result(timeout=timeout)
            
            # 3. 缓存更新：成功返回后刷新内存字典
            _TTL_CACHE[date_str] = {
                "timestamp": current_time,
                "events": events
            }
            return events
            
        except concurrent.futures.TimeoutError:
            print(f"⚠️ 飞书接口请求超时(>{timeout}s)，触发断网保护降级为无事件推演。")
            return []
        except Exception as e:
            print(f"⚠️ 飞书接口请求异常: {str(e)}，触发保护降级为无事件。")
            return []