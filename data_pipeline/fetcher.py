# data_pipeline/fetcher.py
import concurrent.futures
from data_pipeline.local_cache import save_calendar_events

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
    
    save_calendar_events(date_str, events)
    return events

def fetch_events_with_timeout(date_str, open_id=None, injected_token=None, injected_calendar_id=None, timeout=5.0):
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            fetch_events_from_calendar_internal, 
            date_str, open_id, injected_token, injected_calendar_id
        )
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            print(f"⚠️ 飞书接口请求超时(>{timeout}s)，触发断网保护降级为无事件推演。")
            return []
        except Exception as e:
            print(f"⚠️ 飞书接口请求异常: {str(e)}，触发保护降级为无事件。")
            return []