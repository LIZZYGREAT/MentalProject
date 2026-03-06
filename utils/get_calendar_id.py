import json
import logging
import warnings

# 忽略pkg_resources的警告
warnings.filterwarnings("ignore", message="pkg_resources is deprecated as an API")

import lark_oapi as lark
from lark_oapi.api.calendar.v4 import *

# 导入获取令牌的模块
from utils.get_token import FeishuAPI, interactive_get_user_access_token

# 配置日志（只输出到文件，不在控制台显示）
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    handlers=[
                        logging.FileHandler("calendar_id.log")
                    ])
logger = logging.getLogger(__name__)

class CalendarIDFetcher:
    """
    日历ID和open_id获取器类
    用于从飞书API获取用户的主日历ID和open_id
    """
    
    def __init__(self):
        """
        初始化日历ID获取器
        创建飞书API客户端实例用于获取令牌
        """
        self.feishu_api = FeishuAPI()
        
    def get_user_token(self):
        """
        获取有效的用户访问令牌
        尝试从文件加载，如果过期则刷新，如果没有则交互式获取
        
        Returns:
            str: 用户访问令牌access_token
        """
        # 尝试获取令牌信息
        token_info = self.feishu_api.load_token_from_file()
        
        # 检查令牌是否有效
        if token_info and not self.feishu_api.is_token_expired(token_info):
            logger.info("使用有效的缓存令牌")
            return token_info["access_token"]
        
        # 如果有刷新令牌，尝试刷新
        if token_info and "refresh_token" in token_info:
            try:
                logger.info("尝试刷新过期的令牌")
                refreshed_token = self.feishu_api.refresh_user_access_token(token_info["refresh_token"])
                # 保存刷新后的令牌
                self.feishu_api.save_token_to_file(refreshed_token)
                return refreshed_token["access_token"]
            except Exception as e:
                logger.warning(f"刷新令牌失败，需要重新获取: {str(e)}")
        
        # 交互式获取新的令牌（调用get_token.py中的函数）
        logger.info("需要交互式获取新的令牌")
        new_token_info = interactive_get_user_access_token()
        
        if new_token_info:
            logger.info("成功获取用户访问令牌")
            return new_token_info["access_token"]
        else:
            raise Exception("无法获取有效的用户访问令牌")
    
    def get_calendar_info(self, user_token):
        """
        使用用户访问令牌获取主日历信息
        
        Args:
            user_token: 用户访问令牌
            
        Returns:
            dict: 包含日历ID和用户ID信息的字典
        """
        # 创建飞书SDK客户端
        client = lark.Client.builder() \
            .enable_set_token(True) \
            .log_level(lark.LogLevel.ERROR) \
            .build()
        
        # 构造获取主日历的请求对象
        request: PrimaryCalendarRequest = PrimaryCalendarRequest.builder() \
            .user_id_type("open_id") \
            .build()
        
        # 发起请求，使用获取到的用户令牌
        option = lark.RequestOption.builder().user_access_token(user_token).build()
        response: PrimaryCalendarResponse = client.calendar.v4.calendar.primary(request, option)
        
        # 处理失败返回
        if not response.success():
            logger.error(
                f"获取主日历失败，代码: {response.code}, 消息: {response.msg}")
            raise Exception(f"获取主日历失败: {response.msg}")
        
        # 检查响应数据是否为None
        if response.data is None:
            logger.error("响应数据(data)为None")
            # 尝试直接从原始响应中获取数据
            if hasattr(response, 'raw') and hasattr(response.raw, 'content'):
                try:
                    raw_content = json.loads(response.raw.content)
                    # 从原始响应中尝试提取日历信息
                    calendar_info = {
                        "calendar_id": None,
                        "owner_id": None
                    }
                    
                    # 直接从原始响应中提取可能的字段
                    if isinstance(raw_content, dict):
                        # 检查各种可能的数据结构
                        if "data" in raw_content and isinstance(raw_content["data"], dict):
                            data = raw_content["data"]
                            if "calendar" in data:
                                calendar_info["calendar_id"] = data["calendar"].get("calendar_id")
                                calendar_info["owner_id"] = data["calendar"].get("owner_id")
                            elif "calendars" in data:
                                for item in data["calendars"]:
                                    if "calendar" in item:
                                        calendar_info["calendar_id"] = item["calendar"].get("calendar_id")
                                        calendar_info["owner_id"] = item.get("user_id")
                                        break
                        else:
                            calendar_info["calendar_id"] = raw_content.get("calendar_id")
                            calendar_info["owner_id"] = raw_content.get("owner_id")
                    
                    return calendar_info
                except Exception as e:
                    logger.error(f"尝试从原始响应提取数据失败: {str(e)}")
            
            # 如果所有尝试都失败，返回默认值
            return {
                "calendar_id": None,
                "owner_id": None
            }
        
        # 使用SDK的序列化方法
        try:
            calendar_data = lark.JSON.marshal(response.data)
            result = json.loads(calendar_data)
        except Exception as e:
            logger.error(f"序列化或解析数据时出错: {str(e)}")
            result = {}
        
        # 尝试多种可能的数据结构解析
        calendar_info = {
            "calendar_id": None,
            "owner_id": None
        }
        
        # 尝试从可能的结构中提取calendar_id和owner_id
        if result.get("calendar"):
            calendar_info["calendar_id"] = result.get("calendar", {}).get("calendar_id")
            calendar_info["owner_id"] = result.get("calendar", {}).get("owner_id")
        elif result.get("calendars"):
            for item in result.get("calendars", []):
                if item.get("calendar"):
                    calendar_info["calendar_id"] = item.get("calendar", {}).get("calendar_id")
                    calendar_info["owner_id"] = item.get("user_id")
                    break
        else:
            calendar_info["calendar_id"] = result.get("calendar_id")
            calendar_info["owner_id"] = result.get("owner_id")
        
        # 记录解析结果
        logger.info(f"解析后的日历信息: {json.dumps(calendar_info)}")
        
        return calendar_info
    
    def save_calendar_info(self, calendar_info, file_path=None):
        """
        保存日历信息到文件
        只保存必要的信息：calendar_id和open_id
        
        Args:
            calendar_info: 日历信息字典
            file_path: 保存文件路径，默认为data/calendar_info.json
        """
        import os
        
        # 确保data目录存在
        data_dir = "data"
        if not os.path.exists(data_dir):
            os.makedirs(data_dir)
            logger.info(f"创建数据目录: {data_dir}")
        
        # 设置默认文件路径
        if file_path is None:
            file_path = os.path.join(data_dir, "calendar_info.json")
            
        try:
            # 只保存必要的信息
            essential_info = {
                "calendar_id": calendar_info.get("calendar_id"),
                "open_id": calendar_info.get("owner_id")
            }
            
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(essential_info, f, ensure_ascii=False, indent=2)
            logger.info(f"日历信息已保存到 {file_path}")
        except Exception as e:
            logger.error(f"保存日历信息到文件时发生异常: {str(e)}")
            raise

def main():
    """
    主函数，执行获取日历ID的完整流程
    """
    # 创建日历ID获取器实例
    fetcher = CalendarIDFetcher()
    
    try:
        # 获取用户访问令牌
        user_token = fetcher.get_user_token()
        
        if not user_token:
            print("无法获取用户令牌")
            return
        
        # 获取日历信息
        calendar_info = fetcher.get_calendar_info(user_token)
        
        # 验证获取到的信息
        if not calendar_info["calendar_id"] or not calendar_info["owner_id"]:
            print("未获取到有效的日历信息")
            return
        
        # 显示获取到的信息
        print(f"calendar_id={calendar_info['calendar_id']}")
        print(f"open_id={calendar_info['owner_id']}")
        
        # 直接保存到默认文件，不询问用户
        fetcher.save_calendar_info(calendar_info)
        
    except Exception as e:
        logger.error(f"获取日历信息过程中发生错误: {str(e)}")
        print(f"错误: {str(e)}")


def get_calendar_id_by_open_id(open_id=None):
    """
    通过open_id获取对应的calendar_id
    如果没有提供open_id，则获取当前用户的日历信息
    
    Args:
        open_id: 用户的open_id，如果为None则获取当前用户的信息
        
    Returns:
        str: 对应的calendar_id，如果获取失败返回None
    """
    import os
    
    # 首先尝试从本地文件获取
    try:
        calendar_file_path = os.path.join("data", "calendar_info.json")
        if os.path.exists(calendar_file_path):
            with open(calendar_file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                # 如果提供了open_id，检查是否匹配
                if open_id:
                    if data.get("open_id") == open_id:
                        return data.get("calendar_id")
                # 如果没有提供open_id，直接返回
                else:
                    return data.get("calendar_id")
    except Exception as e:
        logger.error(f"读取本地日历信息文件出错: {str(e)}")
    
    # 如果本地文件没有或不匹配，通过API获取
    try:
        fetcher = CalendarIDFetcher()
        user_token = fetcher.get_user_token()
        if not user_token:
            return None
        
        calendar_info = fetcher.get_calendar_info(user_token)
        
        # 保存获取到的信息
        fetcher.save_calendar_info(calendar_info)
        
        # 如果提供了open_id，检查是否匹配
        if open_id and calendar_info.get("owner_id") != open_id:
            logger.warning(f"获取到的open_id {calendar_info.get('owner_id')} 与提供的不匹配 {open_id}")
            return None
        
        return calendar_info.get("calendar_id")
    except Exception as e:
        logger.error(f"通过API获取日历ID失败: {str(e)}")
        return None


if __name__ == "__main__":
    main()