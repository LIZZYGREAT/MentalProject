import json
import warnings
import os
from datetime import datetime, time, timedelta
from typing import List, Dict, Any, Optional

warnings.filterwarnings("ignore", category=UserWarning)
import lark_oapi as lark
from lark_oapi.api.calendar.v4 import *
from utils.get_token import get_user_access_token

def calculate_today_time_range(start_hour: int = 8, end_hour: int = 23) -> tuple:
    print("\n2. 计算查询时间范围...")
    if not 0 <= start_hour <= 23:
        raise ValueError(f"开始小时必须在0-23之间，当前值: {start_hour}")
    if not 0 <= end_hour <= 23:
        raise ValueError(f"结束小时必须在0-23之间，当前值: {end_hour}")
    
    today = datetime.now().date()
    date_str = today.strftime("%Y-%m-%d")
    start_ts = int(datetime.combine(today, time(start_hour, 0)).timestamp())
    end_ts = int(datetime.combine(today, time(end_hour, 59)).timestamp())
    
    print(f"   📅 今天日期: {date_str}")
    return start_ts, end_ts, date_str

def calculate_date_range(start_date: str, end_date: str, start_hour: int = 8, end_hour: int = 23) -> tuple:
    print("\n2. 计算查询时间范围...")
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    
    start_ts = int(datetime.combine(start_dt.date(), time(start_hour, 0)).timestamp())
    end_ts = int(datetime.combine(end_dt.date(), time(end_hour, 59)).timestamp())
    
    print(f"   📅 查询日期范围: {start_date} 至 {end_date}")
    return start_ts, end_ts, start_date, end_date

def extract_event_data(event: Dict[str, Any], query_date_str: Optional[str] = None, date_range: Optional[tuple] = None) -> Dict[str, Any] or None:
    """
    [修复版] 提取事件数据。
    1. 增加星期几校验，过滤非当天的周期性母事件。
    2. 对符合星期几的事件，强制校准日期。
    """
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
    
    # === 核心修复逻辑 ===
    if query_date_str:
        try:
            q_date = datetime.strptime(query_date_str, "%Y-%m-%d")
            
            if start_timestamp:
                orig_dt = datetime.fromtimestamp(start_timestamp)
                
                # [关键校验] 检查“原始事件”和“查询日期”是否为同一个星期几
                # weekday(): 0=周一, 6=周日
                if orig_dt.weekday() != q_date.weekday():
                    # 如果星期几对不上，说明这是飞书返回的该系列的其他母事件，必须丢弃
                    # 例如：查周一，API返回了周三的母事件，这里必须过滤掉
                    return None
                
                # 如果星期几对上了，说明这是当天的课（或者是该系列的第一节课且恰好也是周几）
                # 我们信任 API 的返回（因为我们请求了 date_range），进行强制日期校准
                final_date = query_date_str
                
                # 校准开始时间戳
                new_start_dt = q_date.replace(hour=orig_dt.hour, minute=orig_dt.minute, second=orig_dt.second)
                start_timestamp = int(new_start_dt.timestamp())
                
                # 校准结束时间戳
                if end_timestamp:
                    orig_end_dt = datetime.fromtimestamp(end_timestamp)
                    days_diff = (orig_end_dt.date() - orig_dt.date()).days
                    new_end_dt = (q_date + timedelta(days=days_diff)).replace(
                        hour=orig_end_dt.hour, minute=orig_end_dt.minute, second=orig_end_dt.second
                    )
                    end_timestamp = int(new_end_dt.timestamp())
                
                print(f"  🔄 [周期性修正] 校准事件: {summary} ({orig_dt.date()} -> {q_date.date()})")
        except Exception as e:
            print(f"  ⚠️ 日期校准出错: {e}")
            
    elif date_range:
        # 范围查询逻辑暂保持原样
        if start_timestamp:
            final_date = datetime.fromtimestamp(start_timestamp).strftime("%Y-%m-%d")

    if not final_date and start_timestamp:
        final_date = datetime.fromtimestamp(start_timestamp).strftime("%Y-%m-%d")

    return {
        'date': final_date,
        'start_time': start_time_str,
        'end_time': end_time_str,
        'summary': summary,
        'description': description,
        'actual_start_timestamp': start_timestamp,
        'actual_end_timestamp': end_timestamp
    }

def save_events_to_json(events, filename=None, by_date=True):
    if not by_date:
        if filename is None:
            filename = f"calendar_{datetime.now().strftime('%Y%m%d')}.json"
        output_dir = "calendar_data"
        os.makedirs(output_dir, exist_ok=True)
        filepath = os.path.join(output_dir, filename)
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(events, f, ensure_ascii=False, indent=2)
        print(f"\n✅ 已保存 {len(events)} 条事件 → {filepath}")
        return filepath
    else:
        events_by_date = {}
        saved_files = []
        for event in events:
            date_s = event.get('date')
            if date_s:
                if date_s not in events_by_date: events_by_date[date_s] = []
                events_by_date[date_s].append(event)
        
        for d_str, evs in events_by_date.items():
            fname = f"calendar_{d_str.replace('-', '')}.json"
            out_dir = os.path.join("data", "calendar_data") 
            os.makedirs(out_dir, exist_ok=True)
            fpath = os.path.join(out_dir, fname)
            with open(fpath, 'w', encoding='utf-8') as f:
                json.dump(evs, f, ensure_ascii=False, indent=2)
            print(f"\n✅ 已保存 {len(evs)} 条 {d_str} 的事件 → {fpath}")
            saved_files.append(fpath)
        return saved_files

def display_results(events: List[Dict[str, Any]], date_str: Optional[str] = None, date_range: Optional[tuple] = None) -> None:
    print("\n===========================================")
    print(f"最终结果统计:")
    if date_str:
        print(f"- 查询日期: {date_str}")
    elif date_range:
        start_date, end_date = date_range
        print(f"- 查询日期范围: {start_date} 至 {end_date}")
    
    print(f"- 总共保留 {len(events)} 个有效事件")
    print("===========================================")
    
    if events:
        save_events_to_json(events, by_date=True)
        print(f"\n✅ 任务完成！")
    else:
        print("\n⚠️ 没有找到任何事件")
        print("===========================================")

def get_events_in_date_range(client, token, cal_id, start, end, start_h=8, end_h=23):
    start_ts, end_ts, s_str, e_str = calculate_date_range(start, end, start_h, end_h)
    
    print("\n3. 开始查询日程事件...")
    req = ListCalendarEventRequest.builder().calendar_id(cal_id).page_size(100)\
        .start_time(str(start_ts)).end_time(str(end_ts)).user_id_type("open_id").build()
    
    print("   发送API请求...")
    opt = lark.RequestOption.builder().user_access_token(token).build()
    resp = client.calendar.v4.calendar_event.list(req, opt)
    
    if not resp.success():
        print(f"❌ API Error: {resp.msg}")
        return []
    
    res_list = []
    if hasattr(resp.data, 'items') and resp.data.items:
        print(f"   收到 {len(resp.data.items)} 个原始事件")
        for item in resp.data.items:
            ev_dict = json.loads(lark.JSON.marshal(item))
            # 关键修改：单日查询时，query_date_str 设为具体日期，触发 extract_event_data 里的日期对齐逻辑
            q_date = s_str if s_str == e_str else None
            extracted = extract_event_data(ev_dict, query_date_str=q_date, date_range=(s_str, e_str))
            if extracted:
                res_list.append(extracted)
                print(f"  ✅ 保留: {extracted['summary']}")
            else:
                print(f"  ❌ 过滤: {ev_dict.get('summary', '未知')}")
    else:
        print(f"ℹ️ 无事件")
        
    return res_list