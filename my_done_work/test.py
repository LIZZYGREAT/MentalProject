import warnings
# 过滤pkg_resources弃用警告
warnings.filterwarnings("ignore", message="pkg_resources is deprecated as an API")

import json
import os
from datetime import datetime, time, timedelta
from typing import List, Dict, Any

import lark_oapi as lark
from lark_oapi.api.calendar.v4 import *


# =========================================================
# === 一、时间区间计算函数 ================================
# =========================================================
def get_today_time_range(start_hour=8, end_hour=23):
    """
    获取今天特定时间段的时间戳范围
    例如：获取今天8:00到23:00的时间戳
    """
    # 获取今天的日期
    today = datetime.now().date()
    
    # 创建开始时间（今天的start_hour:00:00）
    start_datetime = datetime.combine(today, time(start_hour, 0, 0))
    
    # 创建结束时间（今天的end_hour:59:59）
    end_datetime = datetime.combine(today, time(end_hour, 59, 59))
    
    # 转换为时间戳
    start_timestamp = int(start_datetime.timestamp())
    end_timestamp = int(end_datetime.timestamp())
    date_str = today.strftime("%Y-%m-%d")
    
    print(f"📅 今天({date_str})的查询范围:")
    print(f"  开始时间: {start_datetime} (时间戳: {start_timestamp})")
    print(f"  结束时间: {end_datetime} (时间戳: {end_timestamp})")
    
    return start_timestamp, end_timestamp, date_str


def get_time_range_for_date(target_date=None, start_hour=8, end_hour=23):
    """
    获取指定日期的时间段时间戳
    如果不指定日期，则使用今天
    """
    # 如果没有指定日期，使用今天
    if target_date is None:
        return get_today_time_range(start_hour, end_hour)
    
    # 创建开始和结束时间
    start_datetime = datetime.combine(target_date, time(start_hour, 0, 0))
    end_datetime = datetime.combine(target_date, time(end_hour, 59, 59))
    
    # 转换为时间戳
    start_timestamp = int(start_datetime.timestamp())
    end_timestamp = int(end_datetime.timestamp())
    date_str = target_date.strftime("%Y-%m-%d")
    
    print(f"📅 {date_str}的查询范围:")
    print(f"  开始时间: {start_datetime} (时间戳: {start_timestamp})")
    print(f"  结束时间: {end_datetime} (时间戳: {end_timestamp})")
    
    return start_timestamp, end_timestamp, date_str


# =========================================================
# === 二、事件提取函数 ====================================
# =========================================================
def extract_event_data(event: Dict[str, Any], query_date_str: str) -> Dict[str, Any] or None:
    """
    从飞书事件对象中提取 summary、时间段、description 等基础字段，并验证事件是否真正在查询日期范围内
    
    关键改进：
    1. 直接验证事件的开始时间戳是否在查询日期当天
    2. 处理跨日期事件的情况
    3. 确保只有真正在今天的事件才会被包含
    """
    # 获取事件标题
    summary = event.get('summary', '').strip()
    if not summary:
        return None

    # 提取描述
    description = event.get('description', '') or ''
    
    # 获取事件实际的开始和结束时间戳
    start_time_obj = event.get('start_time', {})
    end_time_obj = event.get('end_time', {})
    
    start_timestamp = None
    end_timestamp = None
    
    # 尝试获取时间戳（使用更简洁的方式）
    if isinstance(start_time_obj, dict) and 'timestamp' in start_time_obj:
        start_timestamp = start_time_obj['timestamp']
        # 确保时间戳是整数
        if isinstance(start_timestamp, str):
            try:
                start_timestamp = int(start_timestamp)
            except ValueError:
                start_timestamp = None
    
    if isinstance(end_time_obj, dict) and 'timestamp' in end_time_obj:
        end_timestamp = end_time_obj['timestamp']
        # 确保时间戳是整数
        if isinstance(end_timestamp, str):
            try:
                end_timestamp = int(end_timestamp)
            except ValueError:
                end_timestamp = None
    
    # 核心逻辑：验证事件是否在查询日期当天
    if start_timestamp:
        # 将时间戳转换为日期
        event_date_str = datetime.fromtimestamp(start_timestamp).strftime("%Y-%m-%d")
        
        # 只有当事件日期与查询日期完全匹配时才保留
        if event_date_str == query_date_str:
            print(f"  ✅ 事件时间匹配: {summary} (日期: {event_date_str})")
        else:
            # 检查是否是跨日期事件（开始在前一天，结束在查询日期）
            if end_timestamp:
                end_date_str = datetime.fromtimestamp(end_timestamp).strftime("%Y-%m-%d")
                # 如果事件结束在查询日期，则包含它
                if end_date_str == query_date_str:
                    print(f"  ⚠️ 跨日期事件，结束在查询日期: {summary} (开始: {event_date_str}, 结束: {end_date_str})")
                else:
                    print(f"  ❌ 事件不在查询范围内: {summary} (日期: {event_date_str})")
                    return None
            else:
                print(f"  ❌ 事件日期不匹配: {summary} (实际日期: {event_date_str}, 查询日期: {query_date_str})")
                return None
    else:
        print(f"  ❌ 无法获取事件时间戳: {summary}")
        return None
    
    # 格式化时间字符串
    start_time_str = ""
    end_time_str = ""
    
    if start_timestamp:
        try:
            start_time_str = datetime.fromtimestamp(start_timestamp).strftime("%H:%M")
        except:
            pass
    
    if end_timestamp:
        try:
            end_time_str = datetime.fromtimestamp(end_timestamp).strftime("%H:%M")
        except:
            pass

    # 返回提取的事件数据
    return {
        'date': query_date_str,
        'start_time': start_time_str,
        'end_time': end_time_str,
        'summary': summary,
        'description': description,
        'actual_start_timestamp': start_timestamp,
        'actual_end_timestamp': end_timestamp
    }


# =========================================================
# === 三、存储与主流程 ====================================
# =========================================================
def save_events_to_json(events: List[Dict[str, Any]], filename: str = None):
    """保存事件为 JSON 文件"""
    if filename is None:
        current_date = datetime.now().strftime("%Y%m%d")
        filename = f"calendar_{current_date}.json"

    output_dir = "calendar_data"
    os.makedirs(output_dir, exist_ok=True)
    filepath = os.path.join(output_dir, filename)

    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(events, f, ensure_ascii=False, indent=2)

    print(f"\n✅ 已保存 {len(events)} 条事件 → {filepath}")
    return filepath


def create_feishu_client(app_id: str, app_secret: str) -> lark.Client:
    """
    创建飞书客户端
    
    Args:
        app_id: 飞书应用ID
        app_secret: 飞书应用密钥
    
    Returns:
        lark.Client: 飞书客户端实例
    """
    print("\n1. 创建飞书客户端...")
    client = lark.Client.builder() \
        .app_id(app_id) \
        .app_secret(app_secret) \
        .log_level(lark.LogLevel.INFO) \
        .build()
    print("   ✅ 飞书客户端创建成功")
    return client


def calculate_today_time_range(start_hour: int = 8, end_hour: int = 23) -> tuple:
    """
    计算今天特定时间段的时间戳范围
    
    Args:
        start_hour: 开始小时数（默认8点）
        end_hour: 结束小时数（默认23点）
    
    Returns:
        tuple: (开始时间戳, 结束时间戳, 日期字符串)
    """
    print("\n2. 计算查询时间范围...")
    # 直接计算今天特定时间的时间戳
    today = datetime.now().date()
    date_str = today.strftime("%Y-%m-%d")
    
    # 创建开始时间对象
    start_datetime = datetime.combine(today, time(start_hour, 0, 0))
    start_ts = int(start_datetime.timestamp())
    
    # 创建结束时间对象
    end_datetime = datetime.combine(today, time(end_hour, 59, 59))
    end_ts = int(end_datetime.timestamp())
    
    print(f"   📅 今天日期: {date_str}")
    print(f"   ⏰ 查询时间范围: {start_hour}:00-{end_hour}:59")
    print(f"   ⏱️  开始时间: {start_datetime} (时间戳: {start_ts})")
    print(f"   ⏱️  结束时间: {end_datetime} (时间戳: {end_ts})")
    
    return start_ts, end_ts, date_str


def fetch_calendar_events(client: lark.Client, calendar_id: str, 
                        start_timestamp: int, end_timestamp: int, 
                        date_str: str) -> List[Dict[str, Any]]:
    """
    从飞书日历获取事件并进行过滤
    
    Args:
        client: 飞书客户端实例
        calendar_id: 日历ID
        start_timestamp: 开始时间戳
        end_timestamp: 结束时间戳
        date_str: 查询日期字符串（格式：YYYY-MM-DD）
    
    Returns:
        List[Dict[str, Any]]: 过滤后的事件列表
    """
    print("\n3. 开始查询日程事件...")
    print(f"   使用日历ID: {calendar_id}")
    print(f"\n🔍 查询 {date_str} 的日程...")

    # 构建请求
    req = ListCalendarEventRequest.builder() \
        .calendar_id(calendar_id) \
        .page_size(100) \
        .start_time(str(start_timestamp)) \
        .end_time(str(end_timestamp)) \
        .user_id_type("open_id") \
        .build()

    # 发送请求
    print("   发送API请求...")
    resp: ListCalendarEventResponse = client.calendar.v4.calendar_event.list(req)

    if not resp.success():
        print(f"❌ 查询失败: {resp.code} - {resp.msg}")
        return []

    print(f"   ✅ 查询成功，处理返回结果...")
    
    # 提取和过滤事件
    filtered_events = []
    total_events_received = 0
    filtered_out = 0
        
    if hasattr(resp.data, 'items') and resp.data.items:
        total_events_received = len(resp.data.items)
        print(f"   收到 {total_events_received} 个原始事件")
        
        for i, item in enumerate(resp.data.items, 1):
            print(f"   处理事件 {i}/{total_events_received}...")
            event_dict = json.loads(lark.JSON.marshal(item))
            
            # 打印原始事件的时间信息用于调试
            start_time_obj = event_dict.get('start_time', {})
            start_timestamp_val = start_time_obj.get('timestamp')
            if start_timestamp_val:
                try:
                    event_time = datetime.fromtimestamp(int(start_timestamp_val))
                    print(f"     原始事件时间: {event_time} (时间戳: {start_timestamp_val})")
                except:
                    print(f"     无法解析的时间戳: {start_timestamp_val}")
            
            # 提取并验证事件
            extracted = extract_event_data(event_dict, date_str)
            if extracted:
                filtered_events.append(extracted)
                print(f"  ✅ 保留事件: {extracted['summary']} ({extracted['start_time']}–{extracted['end_time']})"),
            else:
                filtered_out += 1
                print(f"  ❌ 过滤事件: {event_dict.get('summary', '无标题')}")
    else:
        print(f"ℹ️ {date_str} 没有找到事件")
    
    print(f"   统计: 收到 {total_events_received} 个事件, 保留 {len(filtered_events)} 个, 过滤 {filtered_out} 个")
    return filtered_events


def display_results(events: List[Dict[str, Any]], date_str: str) -> None:
    """
    显示查询结果并保存数据
    
    Args:
        events: 事件列表
        date_str: 查询日期字符串
    """
    print("\n===========================================")
    print(f"最终结果统计:")
    print(f"- 查询日期: {date_str}")
    print(f"- 总共保留 {len(events)} 个有效事件")
    print("===========================================")
    
    if events:
        file_path = save_events_to_json(events)
        print(f"\n✅ 任务完成！")
    else:
        print("\n⚠️ 没有找到任何事件")
        print("===========================================")


def get_today_calendar_events(app_id: str = "cli_a74daa9319ff500c", 
                             app_secret: str = "3IwKblhV29gurCoAj37oQcInvczvgEx7",
                             calendar_id: str = "feishu.cn_tbUzcWsrFrbgBxGm5dgoqc@group.calendar.feishu.cn",
                             start_hour: int = 8,
                             end_hour: int = 23) -> List[Dict[str, Any]]:
    """
    获取今天的日历事件的主函数
    
    Args:
        app_id: 飞书应用ID
        app_secret: 飞书应用密钥
        calendar_id: 日历ID
        start_hour: 开始小时数
        end_hour: 结束小时数
    
    Returns:
        List[Dict[str, Any]]: 过滤后的事件列表
    """
    print("\n===========================================")
    print("           开始获取日程数据")
    print("===========================================")
    
    try:
        # 1. 创建飞书客户端
        client = create_feishu_client(app_id, app_secret)
        
        # 2. 计算今天的时间范围
        start_ts, end_ts, date_str = calculate_today_time_range(start_hour, end_hour)
        
        # 3. 获取并过滤事件
        events = fetch_calendar_events(client, calendar_id, start_ts, end_ts, date_str)
        
        # 4. 显示结果
        display_results(events, date_str)
        
        return events
        
    except Exception as e:
        print(f"\n❌ 发生错误: {str(e)}")
        import traceback
        traceback.print_exc()
        return []


def main():
    """
    程序入口点
    """
    # 直接调用封装好的函数获取今天的日程
    get_today_calendar_events()


if __name__ == "__main__":
    main()