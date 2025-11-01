import os
import requests
import json
import logging
import time
# 加载.env文件中的环境变量
try:
    from dotenv import load_dotenv
    load_dotenv()
    logging.info("成功加载.env文件")
except ImportError:
    logging.warning("未安装dotenv库，无法加载.env文件，请确保已设置环境变量")


# 配置日志
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    handlers=[
                        logging.FileHandler("login.log"),
                        logging.StreamHandler()
                    ])
logger = logging.getLogger(__name__)


class FeishuAPI:
    """
    飞书API客户端类，用于处理与飞书开放平台的交互
    主要实现获取授权码、获取用户访问令牌以及刷新令牌的功能
    """
    
    def __init__(self, app_id="cli_a74daa9319ff500c", app_secret="3IwKblhV29gurCoAj37oQcInvczvgEx7", redirect_uri=None):
        """
        初始化飞书API客户端
        从环境变量加载配置信息，如果提供了参数则优先使用参数值
        
        Args:
            app_id: 飞书应用的App ID
            app_secret: 飞书应用的App Secret
            redirect_uri: 授权回调地址
        """
        # 优先使用传入的参数，如果没有则从环境变量加载
        # 支持两种环境变量命名方式：直接命名和FEISHU前缀命名
        self.app_id = app_id or os.getenv("FEISHU_APP_ID") or os.getenv("APP_ID")
        self.app_secret = app_secret or os.getenv("FEISHU_APP_SECRET") or os.getenv("APP_SECRET")
        self.redirect_uri = redirect_uri or os.getenv("REDIRECT_URI", "https://open.feishu.cn/api-explorer/loading")
        
        # 验证必要的配置信息
        if not self.app_id:
            raise ValueError("APP_ID或FEISHU_APP_ID必须通过环境变量设置或作为参数传入")
        if not self.app_secret:
            raise ValueError("APP_SECRET或FEISHU_APP_SECRET必须通过环境变量设置或作为参数传入")
    
    def generate_authorize_url(self, scope="auth:user.id:read", state=None):
        """
        生成授权URL，用户需要访问此URL进行授权
        
        Args:
            redirect_uri: 授权回调地址
            scope: 请求的权限范围，默认只请求基本用户信息权限
            state: 维护请求和回调状态的参数，用于防止CSRF攻击
            
        Returns:
            str: 完整的授权URL
        """
        # 飞书授权页面URL
        authorize_url = "https://accounts.feishu.cn/open-apis/authen/v1/authorize"
        
        # 构建查询参数
        params = {
            "client_id": self.app_id,
            "response_type": "code",
            "redirect_uri": self.redirect_uri,
            "scope": scope
        }
        
        # 如果提供了state参数，则添加到查询参数中
        if state:
            params["state"] = state
        
        # 构建完整的URL
        query_string = ""
        for key, value in params.items():
            if query_string:  # 如果不是第一个参数，添加&符号
                query_string += "&"
            query_string += f"{key}={value}"
        
        full_url = f"{authorize_url}?{query_string}"
        logger.info(f"生成授权URL成功: {full_url}")
        return full_url
    
    def get_user_access_token(self, code):
        """
        使用授权码获取用户访问令牌
        
        Args:
            code: 用户授权后获取的授权码
            redirect_uri: 授权回调地址，必须与生成授权URL时使用的地址一致
            
        Returns:
            dict: 包含access_token、refresh_token等信息的字典
        """
        if not code:
            raise ValueError("授权码不能为空")
        
        # 获取用户访问令牌的API地址
        token_url = "https://open.feishu.cn/open-apis/authen/v2/oauth/token"
        
        # 构建请求体
        data = {
            "grant_type": "authorization_code",
            "client_id": self.app_id,
            "client_secret": self.app_secret,
            "code": code,
            "redirect_uri": self.redirect_uri
        }
        
        try:
            # 发送请求获取令牌
            logger.info("开始获取用户访问令牌...")
            response = requests.post(token_url, json=data)
            response_data = response.json()
            
            # 检查请求是否成功
            if response.status_code != 200 or response_data.get("code") != 0:
                error_msg = response_data.get("msg", "未知错误")
                logger.error(f"获取用户访问令牌失败: {error_msg}")
                raise Exception(f"获取用户访问令牌失败: {error_msg}")
            
            # 保存令牌信息
            token_info = {
                "access_token": response_data.get("access_token"),
                "expires_in": response_data.get("expires_in"),
                "refresh_token": response_data.get("refresh_token"),
                "token_type": "Bearer",
                "scope": response_data.get("scope"),
                "timestamp": int(time.time())  # 记录获取时间，用于判断是否过期
            }
            
            logger.info("获取用户访问令牌成功")
            return token_info
            
        except Exception as e:
            logger.error(f"获取用户访问令牌时发生异常: {str(e)}")
            raise
    
    def refresh_user_access_token(self, refresh_token):
        """
        使用刷新令牌获取新的用户访问令牌
        
        Args:
            refresh_token: 用于刷新访问令牌的刷新令牌
            
        Returns:
            dict: 包含新的access_token、refresh_token等信息的字典
        """
        if not refresh_token:
            raise ValueError("刷新令牌不能为空")
        
        # 刷新用户访问令牌的API地址
        refresh_url = "https://open.feishu.cn/open-apis/authen/v2/oauth/token"
        
        # 构建请求体
        data = {
            "grant_type": "refresh_token",
            "client_id": self.app_id,
            "client_secret": self.app_secret,
            "refresh_token": refresh_token
        }
        
        try:
            # 发送请求刷新令牌
            logger.info("开始刷新用户访问令牌...")
            response = requests.post(refresh_url, json=data)
            response_data = response.json()
            
            # 检查请求是否成功
            if response.status_code != 200 or response_data.get("code") != 0:
                error_msg = response_data.get("msg", "未知错误")
                logger.error(f"刷新用户访问令牌失败: {error_msg}")
                raise Exception(f"刷新用户访问令牌失败: {error_msg}")
            
            # 保存新的令牌信息
            new_token_info = {
                "access_token": response_data.get("access_token"),
                "expires_in": response_data.get("expires_in"),
                "refresh_token": response_data.get("refresh_token"),
                "token_type": "Bearer",
                "scope": response_data.get("scope"),
                "timestamp": int(time.time())  # 记录获取时间
            }
            
            logger.info("刷新用户访问令牌成功")
            return new_token_info
            
        except Exception as e:
            logger.error(f"刷新用户访问令牌时发生异常: {str(e)}")
            raise
    
    def save_token_to_file(self, token_info, file_path="user_token.json"):
        """
        将令牌信息保存到文件
        
        Args:
            token_info: 令牌信息字典
            file_path: 保存文件路径，默认user_token.json
        """
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(token_info, f, ensure_ascii=False, indent=2)
            logger.info(f"令牌信息已保存到 {file_path}")
        except Exception as e:
            logger.error(f"保存令牌信息到文件时发生异常: {str(e)}")
            raise
    
    def load_token_from_file(self, file_path="user_token.json"):
        """
        从文件加载令牌信息
        
        Args:
            file_path: 文件路径，默认user_token.json
            
        Returns:
            dict: 令牌信息字典，如果文件不存在或加载失败则返回None
        """
        try:
            if not os.path.exists(file_path):
                logger.warning(f"令牌文件 {file_path} 不存在")
                return None
            
            with open(file_path, "r", encoding="utf-8") as f:
                token_info = json.load(f)
            
            logger.info(f"从 {file_path} 加载令牌信息成功")
            return token_info
            
        except Exception as e:
            logger.error(f"从文件加载令牌信息时发生异常: {str(e)}")
            return None
    
    def is_token_expired(self, token_info, buffer_seconds=300):
        """
        检查令牌是否已过期
        
        Args:
            token_info: 令牌信息字典
            buffer_seconds: 过期缓冲时间（秒），默认提前5分钟认为过期
            
        Returns:
            bool: 如果令牌已过期或即将过期则返回True，否则返回False
        """
        if not token_info:
            return True
        
        # 获取令牌获取时间和过期时间
        token_timestamp = token_info.get("timestamp", 0)
        expires_in = token_info.get("expires_in", 0)
        current_timestamp = int(time.time())
        
        # 计算令牌是否已过期或即将过期
        is_expired = (current_timestamp - token_timestamp) + buffer_seconds >= expires_in
        
        if is_expired:
            logger.info("令牌已过期或即将过期")
        else:
            remaining = expires_in - (current_timestamp - token_timestamp)
            logger.info(f"令牌剩余有效期: {remaining} 秒")
        
        return is_expired


def interactive_get_user_access_token():
    """
    交互式获取用户访问令牌的主函数
    引导用户完成授权流程并获取用户访问令牌
    从环境变量加载APP_ID、APP_SECRET和REDIRECT_URI
    """
    try:
        # 创建飞书API客户端实例（会自动从环境变量加载配置）
        feishu_api = FeishuAPI()
        
        # 询问用户是否需要添加offline_access权限（用于获取refresh_token）
        need_refresh = input("是否需要获取刷新令牌(refresh_token)？(y/n，默认为y): ").strip().lower()
        need_refresh = need_refresh != 'n'  # 默认为True
        
        # 构建权限范围
        scope = "auth:user.id:read"
        if need_refresh:
            scope += " offline_access"
        
        # 显示从环境变量加载的回调地址
        print(f"从环境变量加载的回调地址: {feishu_api.redirect_uri}")
        
        # 生成授权URL
        state = f"auth_{int(time.time())}"  # 生成一个简单的state参数
        authorize_url = feishu_api.generate_authorize_url(scope, state)
        
        print("\n请按照以下步骤进行授权:")
        print(f"1. 复制并在浏览器中打开以下URL:")
        print(f"   {authorize_url}")
        print("2. 登录您的飞书账号并同意授权")
        print("3. 授权成功后，浏览器会跳转到回调地址，并在URL中包含授权码(code参数)")
        print("4. 请从URL中复制code参数的值")
        
        # 获取用户输入的授权码
        code = input("\n请输入您获取到的授权码: ").strip()
        if not code:
            raise ValueError("授权码不能为空")
        
        # 使用授权码获取用户访问令牌
        token_info = feishu_api.get_user_access_token(code)
        
        # 显示获取到的令牌信息
        print("\n✅ 恭喜！成功获取用户访问令牌")
        print(f"访问令牌: {token_info['access_token']}")
        print(f"令牌有效期: {token_info['expires_in']} 秒")
        if token_info.get('refresh_token'):
            print(f"刷新令牌: {token_info['refresh_token']}")
        print(f"权限范围: {token_info['scope']}")
        
        # 询问是否保存令牌信息
        save_token = input("\n是否将令牌信息保存到文件？(y/n，默认为y): ").strip().lower()
        if save_token != 'n':
            file_path = input("请输入保存文件路径（默认: user_token.json）: ").strip()
            file_path = file_path or "user_token.json"
            feishu_api.save_token_to_file(token_info, file_path)
            print(f"令牌信息已保存到: {file_path}")
        
        # 返回获取到的令牌信息
        return token_info
        
    except Exception as e:
        logger.error(f"获取用户访问令牌过程中发生错误: {str(e)}")
        print(f"❌ 错误: {str(e)}")
        return None


def refresh_existing_token():
    """
    刷新现有令牌的函数
    从环境变量加载APP_ID和APP_SECRET
    """
    try:
        # 创建飞书API客户端实例（会自动从环境变量加载配置）
        feishu_api = FeishuAPI()
        
        # 询问是否从文件加载刷新令牌
        load_from_file = input("是否从文件加载刷新令牌？(y/n，默认为y): ").strip().lower()
        refresh_token = None
        
        if load_from_file != 'n':
            file_path = input("请输入令牌文件路径（默认: user_token.json）: ").strip()
            file_path = file_path or "user_token.json"
            token_info = feishu_api.load_token_from_file(file_path)
            if token_info:
                refresh_token = token_info.get("refresh_token")
        
        # 如果没有从文件加载到刷新令牌，则手动输入
        if not refresh_token:
            refresh_token = input("请输入刷新令牌: ").strip()
            if not refresh_token:
                raise ValueError("刷新令牌不能为空")
        
        # 刷新令牌
        new_token_info = feishu_api.refresh_user_access_token(refresh_token)
        
        # 显示刷新后的令牌信息
        print("\n✅ 恭喜！成功刷新用户访问令牌")
        print(f"新的访问令牌: {new_token_info['access_token']}")
        print(f"令牌有效期: {new_token_info['expires_in']} 秒")
        print(f"新的刷新令牌: {new_token_info['refresh_token']}")
        print(f"权限范围: {new_token_info['scope']}")
        
        # 询问是否保存新的令牌信息
        save_token = input("\n是否将新的令牌信息保存到文件？(y/n，默认为y): ").strip().lower()
        if save_token != 'n':
            file_path = input("请输入保存文件路径（默认: user_token.json）: ").strip()
            file_path = file_path or "user_token.json"
            feishu_api.save_token_to_file(new_token_info, file_path)
            print(f"新的令牌信息已保存到: {file_path}")
        
        # 返回刷新后的令牌信息
        return new_token_info
        
    except Exception as e:
        logger.error(f"刷新用户访问令牌过程中发生错误: {str(e)}")
        print(f"❌ 错误: {str(e)}")
        return None


def get_user_access_token(interactive=True):
    """
    获取用户访问令牌的函数，提供给其他模块调用的接口
    
    Args:
        interactive: 是否允许交互式获取令牌，如果为False且没有有效令牌则返回None
        
    Returns:
        dict: 包含access_token等信息的字典，如果获取失败则返回None
    """
    try:
        # 创建飞书API客户端实例
        feishu_api = FeishuAPI()
        
        # 检查是否有已保存的令牌文件
        token_info = feishu_api.load_token_from_file()
        
        # 如果有令牌且未过期，直接返回
        if token_info and not feishu_api.is_token_expired(token_info):
            logger.info("使用有效的缓存令牌")
            return token_info
        
        # 如果有刷新令牌，尝试刷新
        if token_info and "refresh_token" in token_info:
            try:
                logger.info("尝试刷新过期的令牌")
                refreshed_token = feishu_api.refresh_user_access_token(token_info["refresh_token"])
                # 保存刷新后的令牌
                feishu_api.save_token_to_file(refreshed_token)
                return refreshed_token
            except Exception as e:
                logger.warning(f"刷新令牌失败，需要重新获取: {str(e)}")
        
        # 交互式获取新的令牌
        if interactive:
            logger.info("需要重新获取令牌")
            new_token = interactive_get_user_access_token()
            return new_token
        else:
            logger.warning("没有有效令牌且不允许交互式获取")
            return None
            
    except Exception as e:
        logger.error(f"获取用户访问令牌时发生错误: {str(e)}")
        return None

def main():
    """
    主函数，提供交互式菜单
    """
    while True:
        print("\n===== 飞书用户访问令牌获取工具 =====")
        print("1. 获取新的用户访问令牌")
        print("2. 刷新现有令牌")
        print("0. 退出")
        
        choice = input("请选择操作 (0-2): ").strip()
        
        if choice == '1':
            interactive_get_user_access_token()
        elif choice == '2':
            refresh_existing_token()
        elif choice == '0':
            print("感谢使用，再见！")
            break
        else:
            print("无效的选择，请重新输入")


# 如果直接运行此脚本，则执行主函数
if __name__ == "__main__":
    main()