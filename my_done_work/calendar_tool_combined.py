import json
import os
import logging
import warnings
from datetime import datetime, time
from typing import List, Dict, Any

# 忽略pkg_resources弃用警告
warnings.filterwarnings("ignore", category=UserWarning, message="pkg_resources is deprecated as an API")

import lark_oapi as lark
from lark_oapi.api.calendar.v4 import *

# 配置日志
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


class FeishuCalendarTool:
    """
    飞书日历工具类，整合飞书API认证和日历查询功能
    提供两种认证方式：
    1. 使用app_id和app_secret进行应用级认证
    2. 使用user_access_token进行用户级认证
    """
    
    def __init__(self, app_id=None, app_secret=None, user_access_token=None):
        """
        初始化日历工具
        
        Args:
            app_id: 飞书应用ID（应用级认证使用）
            app_secret: 飞书应用密钥（应用级认证使用）
            user_access_token: 用户访问令牌（用户级认证使用）
        """
        self.app_id = app_id or os.getenv("FEISHU_APP_ID") or os.getenv("APP_ID")
        self.app_secret = app_secret or os.getenv("FEISHU_APP_SECRET") or os.getenv("APP_SECRET")
        self.user_access_token = user_access_token
        self.client = None
        
    def create_client(self):
        """
        创建飞书客户端，可以根据提供的凭据选择不同的认证方式
        """
        print("\n1. 创建飞书客户端...")
        
        # 创建客户端构建器
        builder = lark.Client.builder().log_level(lark.LogLevel.INFO)
        
        # 根据提供的凭据选择认证方式
        if self.user_access_token:
            # 使用用户访问令牌的方式
            builder.enable_set_token(True)
            print("   使用用户访问令牌进行认证")
        elif self.app_id and self.app_secret:
            # 使用应用ID和密钥的方式
            builder.app_id(self.app_id).app_secret(self.app_secret)
            print("   使用应用ID和密钥进行认证")
        else:
            raise ValueError("请提供有效的认证凭据：user_access_token 或 (app_id 和 app_secret)")
        
        # 构建客户端
        self.client = builder.build()
        print("   ✅ 飞书客户端创建成功")
        return self.client
    
    def calculate_today_time_range(self, start_hour=8, end_hour=23):
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
    
    def extract_event_data(self, event, query_date_str):
        """
        从飞书事件对象中提取所需信息
        
        Args:
            event: 原始事件对象
            query_date_str: 查询日期字符串（格式：YYYY-MM-DD）
        
        Returns:
            dict: 提取后的事件信息，或None表示无效事件
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
            if isinstance(start_timestamp, str):
                try:
                    start_timestamp = int(start_timestamp)
                except ValueError:
                    start_timestamp = None
        
        if isinstance(end_time_obj, dict) and 'timestamp' in end_time_obj:
            end_timestamp = end_time_obj['timestamp']
            if isinstance(end_timestamp, str):
                try:
                    end_timestamp = int(end_timestamp)
                except ValueError:
                    end_timestamp = None
        
        # 验证事件是否在查询日期当天
        if start_timestamp:
            event_date_str = datetime.fromtimestamp(start_timestamp).strftime("%Y-%m-%d")
            
            # 只有当事件日期与查询日期完全匹配时才保留
            if event_date_str != query_date_str:
                # 检查是否是跨日期事件
                if end_timestamp:
                    end_date_str = datetime.fromtimestamp(end_timestamp).strftime("%Y-%m-%d")
                    if end_date_str != query_date_str:
                        return None
                else:
                    return None
        else:
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
    
    def fetch_calendar_events(self, calendar_id, start_timestamp, end_timestamp, date_str):
        """
        从飞书日历获取事件并进行过滤
        
        Args:
            calendar_id: 日历ID
            start_timestamp: 开始时间戳
            end_timestamp: 结束时间戳
            date_str: 查询日期字符串
        
        Returns:
            List[Dict]: 过滤后的事件列表
        """
        if not self.client:
            self.create_client()
        
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

        # 发送请求，根据认证方式选择不同的选项
        print("   发送API请求...")
        if self.user_access_token:
            # 使用用户访问令牌
            option = lark.RequestOption.builder().user_access_token(self.user_access_token).build()
            resp = self.client.calendar.v4.calendar_event.list(req, option)
        else:
            # 使用应用ID和密钥
            resp = self.client.calendar.v4.calendar_event.list(req)

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
                
                # 提取并验证事件
                extracted = self.extract_event_data(event_dict, date_str)
                if extracted:
                    filtered_events.append(extracted)
                    print(f"  ✅ 保留事件: {extracted['summary']} ({extracted['start_time']}–{extracted['end_time']})")
                else:
                    filtered_out += 1
                    print(f"  ❌ 过滤事件: {event_dict.get('summary', '无标题')}")
        else:
            print(f"ℹ️ {date_str} 没有找到事件")
        
        print(f"   统计: 收到 {total_events_received} 个事件, 保留 {len(filtered_events)} 个, 过滤 {filtered_out} 个")
        return filtered_events
    
    def save_events_to_json(self, events, filename=None):
        """
        保存事件列表到JSON文件
        
        Args:
            events: 事件列表
            filename: 文件名，如果不提供则自动生成
        
        Returns:
            str: 保存的文件路径
        """
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
    
    def get_today_events(self, calendar_id=None, start_hour=8, end_hour=23):
        """
        获取今天的日历事件
        
        Args:
            calendar_id: 日历ID，如果不提供则使用默认值
            start_hour: 开始小时数
            end_hour: 结束小时数
        
        Returns:
            List[Dict]: 过滤后的事件列表
        """
        if not calendar_id:
            # 使用默认日历ID
            calendar_id = "feishu.cn_tbUzcWsrFrbgBxGm5dgoqc@group.calendar.feishu.cn"
        
        print("\n===========================================")
        print("           开始获取日程数据")
        print("===========================================")
        
        try:
            # 创建客户端
            self.create_client()
            
            # 计算今天的时间范围
            start_ts, end_ts, date_str = self.calculate_today_time_range(start_hour, end_hour)
            
            # 获取并过滤事件
            events = self.fetch_calendar_events(calendar_id, start_ts, end_ts, date_str)
            
            # 显示结果
            print("\n===========================================")
            print(f"最终结果统计:")
            print(f"- 查询日期: {date_str}")
            print(f"- 总共保留 {len(events)} 个有效事件")
            print("===========================================")
            
            if events:
                file_path = self.save_events_to_json(events)
                print(f"\n✅ 任务完成！")
            else:
                print("\n⚠️ 没有找到任何事件")
                print("===========================================")
            
            return events
            
        except Exception as e:
            print(f"\n❌ 发生错误: {str(e)}")
            import traceback
            traceback.print_exc()
            return []


def example_with_app_auth():
    """
    使用应用ID和密钥进行认证的示例
    """
    print("\n==== 使用应用ID和密钥认证示例 ====")
    
    # 创建日历工具实例（应用级认证）
    calendar_tool = FeishuCalendarTool(
        app_id="cli_a74daa9319ff500c",  # 替换为实际的应用ID
        app_secret="3IwKblhV29gurCoAj37oQcInvczvgEx7"  # 替换为实际的应用密钥
    )
    
    # 获取今天的事件
    events = calendar_tool.get_today_events()
    return events


def example_with_user_token():
    """
    使用用户访问令牌进行认证的示例
    """
    print("\n==== 使用用户访问令牌认证示例 ====")
    
    # 创建日历工具实例（用户级认证）
    calendar_tool = FeishuCalendarTool(
        user_access_token="u-79KlBMOR55I8N6d5viimMBh559n0g0iXVO005lyEabgM"  # 替换为实际的用户访问令牌
    )
    
    # 获取今天的事件
    events = calendar_tool.get_today_events()
    return events


def main():
    """
    程序入口点，展示两种认证方式的使用
    """
    # 选择认证方式
    # 方式1: 使用应用ID和密钥
    example_with_app_auth()
    
    # 方式2: 使用用户访问令牌（如果需要使用这种方式，请取消下面的注释）
    # example_with_user_token()


if __name__ == "__main__":
    main()