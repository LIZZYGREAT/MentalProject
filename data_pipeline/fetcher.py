# data_pipeline/fetcher.py
import concurrent.futures
import json
import os
import time
from settings.model_defaults import (
    CACHE_EXPIRY_SECONDS,
    FEISHU_REQUEST_TIMEOUT_SECONDS,
    BASE_DATA_DIR,
    CALENDAR_INFO_FILE,
)

# ==========================================
# 轻量级 TTL (Time-To-Live) 内存缓存
# 结构: { "YYYY-MM-DD": {"timestamp": float, "events": list} }
# ==========================================
_TTL_CACHE = {}


def _mask(value):
    if not value:
        return "empty"
    if len(value) <= 12:
        return value
    return f"{value[:6]}...{value[-4:]}"


def _load_calendar_id_from_file(open_id=None):
    path = os.path.join(BASE_DATA_DIR, CALENDAR_INFO_FILE)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if open_id and data.get("open_id") and data.get("open_id") != open_id:
            return None
        return data.get("calendar_id")
    except Exception as e:
        print(f"读取本地 calendar_info 失败: {e}")
        return None


def _resolve_calendar_id(open_id=None, injected_calendar_id=None):
    from utils.get_token import load_feishu_env

    load_feishu_env()
    calendar_id = injected_calendar_id or os.getenv("FEISHU_CALENDAR_ID") or _load_calendar_id_from_file(open_id)
    if calendar_id:
        print(f"使用 calendar_id: {_mask(calendar_id)}")
        return calendar_id

    print("未配置 FEISHU_CALENDAR_ID，尝试通过主日历接口获取 calendar_id...")
    from utils.get_calendar_id import CalendarIDFetcher
    fetcher = CalendarIDFetcher()
    calendar_id = fetcher.get_calendar_id(open_id)
    if calendar_id:
        print(f"主日历 calendar_id 获取成功: {_mask(calendar_id)}")
    return calendar_id

def fetch_events_from_calendar_internal(date_str, open_id=None, injected_token=None, injected_calendar_id=None):
    from utils.get_token import get_user_access_token
    from utils.calendar_tool import get_events_in_date_range
    
    access_token = injected_token
    if not access_token:
        token_info = get_user_access_token(interactive=False)
        if not token_info:
            print("无法获取用户 Token, 请先运行 get_token.py 授权")
            return []
        access_token = token_info["access_token"]
        
    calendar_id = _resolve_calendar_id(open_id=open_id, injected_calendar_id=injected_calendar_id)
    if not calendar_id:
        print("无法获取 calendar_id")
        return []

    request_timeout = max(1.0, FEISHU_REQUEST_TIMEOUT_SECONDS - 1.0)
    events = get_events_in_date_range(access_token, calendar_id, date_str, date_str, request_timeout=request_timeout)
    
    return events

def fetch_events_with_timeout(date_str, open_id=None, injected_token=None, injected_calendar_id=None, timeout=FEISHU_REQUEST_TIMEOUT_SECONDS, force_refresh=False):
    """带 TTL 缓存与超时的日历拉取；超时或异常返回空列表。"""
    global _TTL_CACHE
    
    current_time = time.time()
    
    # 1. 拦截层：检查 TTL 缓存 (附带 force_refresh 主动击穿)
    if not force_refresh and date_str in _TTL_CACHE:
        cached_data = _TTL_CACHE[date_str]
        age = current_time - cached_data["timestamp"]
        if age < CACHE_EXPIRY_SECONDS:
            print(f"缓存命中: 获取 {date_str} 的日程 (剩余 {int(CACHE_EXPIRY_SECONDS - age)}s)")
            return cached_data["events"]

    if force_refresh:
        print("强制刷新: 发起网络请求...")

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
            print(f"飞书接口请求超时(>{timeout}s)，降级为空事件。")
            return []
        except Exception as e:
            print(f"飞书接口请求异常: {str(e)}，降级为空事件。")
            return []
