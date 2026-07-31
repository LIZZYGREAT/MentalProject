import json
import logging
import warnings
import requests

# 忽略pkg_resources的警告
warnings.filterwarnings("ignore", message="pkg_resources is deprecated as an API")

# 导入获取令牌的模块
from utils.get_token import FeishuAPI, interactive_get_user_access_token
from settings.model_defaults import BASE_DATA_DIR, CALENDAR_INFO_FILE, FEISHU_REQUEST_TIMEOUT_SECONDS


def _default_calendar_info_path() -> str:
    """Return the local calendar-info file path from centralized settings."""
    import os
    return os.path.join(BASE_DATA_DIR, CALENDAR_INFO_FILE)

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
        try:
            url = "https://open.feishu.cn/open-apis/calendar/v4/calendars/primary"
            headers = {"Authorization": f"Bearer {user_token}"}
            params = {"user_id_type": "open_id"}
            response = requests.post(
                url,
                headers=headers,
                params=params,
                timeout=max(1.0, FEISHU_REQUEST_TIMEOUT_SECONDS - 1.0),
            )
            payload = response.json()
        except Exception as e:
            logger.error(f"获取主日历请求失败: {str(e)}")
            raise

        if response.status_code != 200 or payload.get("code") != 0:
            msg = payload.get("msg", "未知错误")
            logger.error(f"获取主日历失败，HTTP: {response.status_code}, code: {payload.get('code')}, msg: {msg}")
            raise Exception(f"获取主日历失败: {msg}")
        
        # 尝试多种可能的数据结构解析
        calendar_info = {
            "calendar_id": None,
            "owner_id": None,
            "summary": None,
            "role": None,
            "type": None,
        }

        result = payload.get("data") or {}
        calendar_payload = None
        
        # 尝试从可能的结构中提取calendar_id和owner_id
        if result.get("calendar"):
            calendar_payload = result.get("calendar", {})
        elif result.get("calendars"):
            for item in result.get("calendars", []):
                if item.get("calendar"):
                    calendar_payload = item.get("calendar", {})
                    calendar_info["owner_id"] = item.get("user_id")
                    break
        else:
            calendar_payload = result

        calendar_payload = calendar_payload or {}
        calendar_info["calendar_id"] = calendar_payload.get("calendar_id")
        calendar_info["owner_id"] = (
            calendar_info["owner_id"] or calendar_payload.get("owner_id")
        )
        calendar_info["summary"] = calendar_payload.get("summary")
        calendar_info["role"] = calendar_payload.get("role")
        calendar_info["type"] = calendar_payload.get("type")
        
        # 记录解析结果
        logger.info(f"解析后的日历信息: {json.dumps(calendar_info)}")
        
        return calendar_info

    def get_calendar_id(self, open_id=None):
        """Return calendar_id from local cache or the primary calendar API."""
        calendar_id = get_calendar_id_by_open_id(open_id)
        return calendar_id
    
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
        data_dir = BASE_DATA_DIR
        if not os.path.exists(data_dir):
            os.makedirs(data_dir)
            logger.info(f"创建数据目录: {data_dir}")
        
        # 设置默认文件路径
        if file_path is None:
            file_path = _default_calendar_info_path()
            
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
        calendar_file_path = _default_calendar_info_path()
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
