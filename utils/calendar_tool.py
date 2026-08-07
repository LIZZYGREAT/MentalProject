import json
import warnings
import os
from datetime import datetime, time, timedelta
from typing import List, Dict, Any, Optional

warnings.filterwarnings("ignore", category=UserWarning)
import requests
from utils.get_token import get_user_access_token

def calculate_today_time_range(start_hour: int = 8, end_hour: int = 23) -> tuple:
    if not 0 <= start_hour <= 23:
        raise ValueError(f"开始小时必须在0-23之间，当前值: {start_hour}")
    if not 0 <= end_hour <= 23:
        raise ValueError(f"结束小时必须在0-23之间，当前值: {end_hour}")
    
    today = datetime.now().date()
    date_str = today.strftime("%Y-%m-%d")
    start_ts = int(datetime.combine(today, time(start_hour, 0)).timestamp())
    end_ts = int(datetime.combine(today, time(end_hour, 59)).timestamp())
    
    print(f"今天日期: {date_str}")
    return start_ts, end_ts, date_str

def calculate_date_range(start_date: str, end_date: str, start_hour: int = 8, end_hour: int = 23) -> tuple:
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    
    start_ts = int(datetime.combine(start_dt.date(), time(start_hour, 0)).timestamp())
    end_ts = int(datetime.combine(end_dt.date(), time(end_hour, 59)).timestamp())
    
    print(f"查询日期范围: {start_date} 至 {end_date}")
    return start_ts, end_ts, start_date, end_date

def extract_event_data(event: Dict[str, Any], query_date_str: Optional[str] = None, date_range: Optional[tuple] = None) -> Dict[str, Any] or None:
    """解析飞书日历事件为统一字段；周期性事件按 query_date_str 校验星期并校准日期。"""
    if event.get("status") == "cancelled":
        return None

    summary = event.get('summary', '').strip()
    if not summary:
        return None

    description = event.get('description', '') or ''
    start_time_obj = event.get('start_time', {})
    end_time_obj = event.get('end_time', {})
    
    start_timestamp = None
    end_timestamp = None
    
    if isinstance(start_time_obj, dict) and 'timestamp' in start_time_obj:
        start_timestamp = int(start_time_obj['timestamp'])
    if isinstance(end_time_obj, dict) and 'timestamp' in end_time_obj:
        end_timestamp = int(end_time_obj['timestamp'])
    
    start_time_str = ""
    end_time_str = ""
    if start_timestamp:
        start_time_str = datetime.fromtimestamp(start_timestamp).strftime("%H:%M")
    if end_timestamp:
        end_time_str = datetime.fromtimestamp(end_timestamp).strftime("%H:%M")

    final_date = None
    
    if query_date_str:
        try:
            q_date = datetime.strptime(query_date_str, "%Y-%m-%d")
            
            if start_timestamp:
                orig_dt = datetime.fromtimestamp(start_timestamp)
                
                if orig_dt.weekday() != q_date.weekday():
                    return None
                
                final_date = query_date_str
                
                new_start_dt = q_date.replace(hour=orig_dt.hour, minute=orig_dt.minute, second=orig_dt.second)
                start_timestamp = int(new_start_dt.timestamp())
                
                if end_timestamp:
                    orig_end_dt = datetime.fromtimestamp(end_timestamp)
                    days_diff = (orig_end_dt.date() - orig_dt.date()).days
                    new_end_dt = (q_date + timedelta(days=days_diff)).replace(
                        hour=orig_end_dt.hour, minute=orig_end_dt.minute, second=orig_end_dt.second
                    )
                    end_timestamp = int(new_end_dt.timestamp())
                
                print(f"  [周期性修正] {summary} ({orig_dt.date()} -> {q_date.date()})")
        except Exception as e:
            print(f"  日期校准出错: {e}")
            
    elif date_range:
        if start_timestamp:
            final_date = datetime.fromtimestamp(start_timestamp).strftime("%Y-%m-%d")

    if not final_date and start_timestamp:
        final_date = datetime.fromtimestamp(start_timestamp).strftime("%Y-%m-%d")

    return {
        'id': event.get('event_id') or event.get('id'),
        'event_id': event.get('event_id') or event.get('id'),
        'date': final_date,
        'start_time': start_time_str,
        'end_time': end_time_str,
        'summary': summary,
        'description': description,
        'status': event.get('status') or 'confirmed',
        'recurrence': event.get('recurrence'),
        'actual_start_timestamp': start_timestamp,
        'actual_end_timestamp': end_timestamp
    }

def save_events_to_json(events, filename=None, by_date=True):
    """仅做控制台记录，不再向本地写入任何 JSON 文件"""
    if not events:
        return []
        
    if not by_date:
        print(f"内存中已就绪 {len(events)} 条事件")
        return ["memory_only"]
    else:
        events_by_date = {}
        for event in events:
            date_s = event.get('date')
            if date_s:
                if date_s not in events_by_date: events_by_date[date_s] = []
                events_by_date[date_s].append(event)
        
        for d_str, evs in events_by_date.items():
            print(f"{d_str} 的 {len(evs)} 条事件已在内存就绪")
        return ["memory_only"]

def display_results(events: List[Dict[str, Any]], date_str: Optional[str] = None, date_range: Optional[tuple] = None) -> None:
    print(f"最终结果统计:")
    if date_str:
        print(f"查询日期: {date_str}")
    elif date_range:
        start_date, end_date = date_range
        print(f"查询日期范围: {start_date} 至 {end_date}")
    
    print(f"总共保留 {len(events)} 个有效事件")
    
    if events:
        save_events_to_json(events, by_date=True)
        print("任务完成。")
    else:
        print("没有找到任何事件")

def get_events_in_date_range(token, cal_id, start, end, start_h=8, end_h=23, request_timeout=4.0):
    """Fetch calendar events with direct HTTP requests.

    This avoids importing the heavy lark_oapi SDK inside the 5s fetch timeout.
    """
    start_ts, end_ts, s_str, e_str = calculate_date_range(start, end, start_h, end_h)
    
    print("开始查询日程事件...")
    if not token:
        print("缺少 user_access_token")
        return []
    if not cal_id:
        print("缺少 calendar_id")
        return []

    res_list = []
    url = f"https://open.feishu.cn/open-apis/calendar/v4/calendars/{cal_id}/events"
    headers = {"Authorization": f"Bearer {token}"}
    params = {
        "start_time": str(start_ts),
        "end_time": str(end_ts),
        "page_size": 100,
        "user_id_type": "open_id",
    }

    page_count = 0
    while True:
        print("发送API请求...")
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=request_timeout)
            payload = resp.json()
        except requests.Timeout:
            raise TimeoutError(f"飞书日历事件请求超时(>{request_timeout}s)")
        except Exception as e:
            print(f"飞书日历事件请求失败: {e}")
            return []

        if resp.status_code != 200 or payload.get("code") != 0:
            print(f"API Error: http={resp.status_code}, code={payload.get('code')}, msg={payload.get('msg')}")
            return []

        data = payload.get("data") or {}
        items = data.get("items") or []
        page_count += 1
        print(f"第 {page_count} 页收到 {len(items)} 个原始事件")
        for ev_dict in items:
            q_date = s_str if s_str == e_str else None
            extracted = extract_event_data(ev_dict, query_date_str=q_date, date_range=(s_str, e_str))
            if extracted:
                res_list.append(extracted)
                print(f"保留: {extracted['summary']}")
            else:
                if ev_dict.get("status") == "cancelled":
                    print("跳过取消事件")
                else:
                    print(f"过滤: {ev_dict.get('summary', '未知')}")

        if not data.get("has_more"):
            break
        page_token = data.get("page_token")
        if not page_token:
            break
        params["page_token"] = page_token

    if not res_list:
        print("无事件")
        
    return res_list
