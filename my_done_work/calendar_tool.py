import json
import warnings
import os
from datetime import datetime, time
from typing import List, Dict, Any

# 忽略pkg_resources弃用警告
warnings.filterwarnings("ignore", category=UserWarning, message="pkg_resources is deprecated as an API")

import lark_oapi as lark
from lark_oapi.api.calendar.v4 import *

# 导入get_token.py中的功能，用于获取用户访问令牌
from get_token import get_user_access_token

# SDK 使用说明: `https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/server-side-sdk/python--sdk/preparations-before-development`
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

def extract_event_data(event: Dict[str, Any], query_date_str: str) -> Dict[str, Any] or None:
    """
    从飞书事件对象中提取 summary、时间段、description 等基础字段，并验证事件是否真正在查询日期范围内
    
    Args:
        event: 事件对象字典
        query_date_str: 查询日期字符串（格式：YYYY-MM-DD）
    
    Returns:
        有效的事件数据字典，如果事件不在查询范围内则返回None
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
    
    # 尝试获取时间戳
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

def main():
    print("\n===========================================")
    print("           开始获取日程数据")
    print("===========================================")
    
    try:
        # 首先获取用户访问令牌
        print("正在获取用户访问令牌...")
        token_info = get_user_access_token(interactive=True)  # 交互式获取令牌
        
        if not token_info or "access_token" not in token_info:
            print("获取用户访问令牌失败，程序退出")
            return
        
        # 从令牌信息中提取用户访问令牌
        user_access_token = token_info["access_token"]
        print(f"成功获取用户访问令牌: {user_access_token[:20]}...")
        
        # 创建client
        print("正在创建飞书客户端...")
        client = lark.Client.builder() \
            .enable_set_token(True) \
            .log_level(lark.LogLevel.INFO) \
            .build()
        print("飞书客户端创建成功")
        
        # 计算今天的时间范围
        start_ts, end_ts, date_str = calculate_today_time_range(start_hour=8, end_hour=23)
        
        # 构造请求对象
        print("\n3. 开始查询日程事件...")
        calendar_id = "feishu.cn_tbUzcWsrFrbgBxGm5dgoqc@group.calendar.feishu.cn"  # 日历ID
        page_size = 100  # 每页查询的事件数量
        
        print(f"   使用日历ID: {calendar_id}")
        print(f"\n🔍 查询 {date_str} 的日程...")

        request: ListCalendarEventRequest = ListCalendarEventRequest.builder() \
            .calendar_id(calendar_id) \
            .page_size(page_size) \
            .start_time(str(start_ts)) \
            .end_time(str(end_ts)) \
            .user_id_type("open_id") \
            .build()
        
        # 发起请求
        print("   发送API请求...")
        option = lark.RequestOption.builder().user_access_token(user_access_token).build()
        response: ListCalendarEventResponse = client.calendar.v4.calendar_event.list(request, option)
        
        # 处理失败返回
        if not response.success():
            print(f"❌ API请求失败!")
            lark.logger.error(
                f"client.calendar.v4.calendar_event.list failed, code: {response.code}, msg: {response.msg}, log_id: {response.get_log_id()}")
            # 输出原始响应内容以便调试
            if hasattr(response, 'raw') and hasattr(response.raw, 'content'):
                try:
                    raw_content = json.loads(response.raw.content)
                    print(f"原始响应内容: {json.dumps(raw_content, indent=2, ensure_ascii=False)}")
                except:
                    print(f"原始响应内容: {response.raw.content}")
            return
        
        print(f"   ✅ 查询成功，处理返回结果...")
        
        # 提取和过滤事件
        filtered_events = []
        total_events_received = 0
        filtered_out = 0
            
        if hasattr(response.data, 'items') and response.data.items:
            total_events_received = len(response.data.items)
            print(f"   收到 {total_events_received} 个原始事件")
            
            for i, item in enumerate(response.data.items, 1):
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
        
        # 显示结果
        display_results(filtered_events, date_str)
        
    except Exception as e:
        print(f"\n❌ 发生错误: {str(e)}")
        import traceback
        traceback.print_exc()
    finally:
        print("\n程序已结束")


if __name__ == "__main__":
    print("===== 飞书日历查询工具 =====")
    print("此工具将获取用户访问令牌并查询指定时间范围内的日历事件")
    try:
        main()
    except KeyboardInterrupt:
        print("\n程序被用户中断")
    except Exception as e:
        print(f"\n程序运行出错: {str(e)}")
        import traceback
        traceback.print_exc()
    finally:
        print("\n程序已结束")