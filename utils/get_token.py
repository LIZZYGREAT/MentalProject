import os
import requests
import json
import logging
import tempfile
import threading
import time
from contextlib import contextmanager
from urllib.parse import urlencode, urlparse
from settings.model_defaults import (
    APP_DEFAULT_HOST,
    APP_DEFAULT_PORT,
    BASE_DATA_DIR,
    DEFAULT_CALLBACK_PATH,
    FEISHU_REQUEST_TIMEOUT_SECONDS,
    TOKEN_EXPIRY_BUFFER_SECONDS,
    USER_TOKEN_FILE,
)


def _project_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))


def _candidate_env_paths():
    root = _project_root()
    return [
        os.path.join(root, ".env"),
        os.path.join(root, "info", ".env"),
    ]


def _load_env_fallback(path: str) -> bool:
    """Load simple KEY=VALUE lines without requiring python-dotenv."""
    if not os.path.exists(path):
        return False
    loaded = False
    with open(path, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
                loaded = True
    return loaded


def load_feishu_env() -> None:
    """Load Feishu credentials from project .env locations.

    The project historically stores secrets under ``info/.env``. ``load_dotenv()``
    only searches from the current working directory, so the web app could miss
    credentials and return an empty auth link. This function checks both common
    locations and has a tiny fallback parser when python-dotenv is absent.
    """
    loaded_any = False
    try:
        from dotenv import load_dotenv
        for path in _candidate_env_paths():
            if os.path.exists(path):
                loaded_any = load_dotenv(path, override=False) or loaded_any
    except ImportError:
        for path in _candidate_env_paths():
            loaded_any = _load_env_fallback(path) or loaded_any

    if loaded_any:
        logging.info("成功加载飞书环境变量")
    else:
        logging.warning("未发现可加载的 .env 文件，请确认根目录 .env 或 info/.env 存在")


load_feishu_env()


# 配置日志
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    handlers=[
                        logging.FileHandler("login.log"),
                        logging.StreamHandler()
                    ])
logger = logging.getLogger(__name__)

DEFAULT_FEISHU_OAUTH_SCOPES = (
    "auth:user.id:read",
    "offline_access",
    "calendar:calendar:readonly",
)
REAUTHORIZE_ERROR_CODES = {20026, 20037, 20064, 20073}
REFRESH_CONFIGURATION_ERROR_CODES = {20024, 20074}
_TOKEN_LOCKS = {}
_TOKEN_LOCKS_GUARD = threading.Lock()


class FeishuAPIError(RuntimeError):
    """Feishu OAuth error with a machine-readable error code."""

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class FeishuAuthorizationRequired(RuntimeError):
    """The saved authorization cannot be reused or refreshed."""


def _token_file_lock(file_path):
    normalized_path = os.path.abspath(file_path)
    with _TOKEN_LOCKS_GUARD:
        return _TOKEN_LOCKS.setdefault(normalized_path, threading.Lock())


@contextmanager
def _interprocess_token_file_lock(file_path, timeout_seconds=None):
    """Serialize one-time refresh-token rotation across server processes."""
    absolute_path = os.path.abspath(file_path)
    lock_path = f"{absolute_path}.lock"
    os.makedirs(os.path.dirname(absolute_path), exist_ok=True)
    timeout_seconds = float(
        timeout_seconds or max(30.0, FEISHU_REQUEST_TIMEOUT_SECONDS * 2)
    )
    lock_file = open(lock_path, "a+b")
    acquired = False
    try:
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"\0")
            lock_file.flush()

        if os.name == "nt":
            import msvcrt

            deadline = time.monotonic() + timeout_seconds
            while True:
                lock_file.seek(0)
                try:
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                    acquired = True
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("等待飞书 token 刷新锁超时")
                    time.sleep(0.05)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            acquired = True

        yield
    finally:
        if acquired:
            lock_file.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()


def _default_token_path() -> str:
    """Return the local token file path from centralized settings."""
    return os.path.join(BASE_DATA_DIR, USER_TOKEN_FILE)


def _validate_redirect_uri(redirect_uri: str) -> str:
    """Reject OAuth callbacks that cannot return to this application's route."""
    value = str(redirect_uri or "").strip()
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(
            "FEISHU_REDIRECT_URI 必须是完整的 http(s) 回调地址，例如 "
            "http://127.0.0.1:5000/callback"
        )
    if parsed.hostname in {"open.feishu.cn", "accounts.feishu.cn"}:
        raise ValueError(
            "FEISHU_REDIRECT_URI 不能指向飞书 API 调试台；它必须指向本项目的 "
            f"{DEFAULT_CALLBACK_PATH} 回调接口"
        )
    if (
        os.getenv("APP_ENV", "development").strip().lower() == "production"
        and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    ):
        raise ValueError(
            "生产环境的 FEISHU_REDIRECT_URI 必须使用用户可访问的公网 HTTPS 域名，"
            "不能使用 localhost 或 127.0.0.1"
        )
    if parsed.path != DEFAULT_CALLBACK_PATH or parsed.query or parsed.fragment:
        raise ValueError(
            "FEISHU_REDIRECT_URI 必须精确指向本项目回调路径 "
            f"{DEFAULT_CALLBACK_PATH}，且不能包含查询参数或 # 片段"
        )
    return value


class FeishuAPI:
    """
    飞书API客户端类，用于处理与飞书开放平台的交互
    主要实现获取授权码、获取用户访问令牌以及刷新令牌的功能
    """
    
    def __init__(self, app_id=None, app_secret=None, redirect_uri=None, require_secret=True):
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
        default_redirect = f"http://{APP_DEFAULT_HOST}:{APP_DEFAULT_PORT}{DEFAULT_CALLBACK_PATH}"
        configured_redirect = (
            redirect_uri
            or os.getenv("FEISHU_REDIRECT_URI")
            or os.getenv("REDIRECT_URI")
            or default_redirect
        )
        self.redirect_uri = _validate_redirect_uri(configured_redirect)
        
        # 验证必要的配置信息
        if not self.app_id:
            raise ValueError("APP_ID或FEISHU_APP_ID必须通过环境变量设置或作为参数传入")
        if require_secret and not self.app_secret:
            raise ValueError("APP_SECRET或FEISHU_APP_SECRET必须通过环境变量设置或作为参数传入")
    
    def generate_authorize_url(self, scope=None, state=None):
        """
        生成授权URL，用户需要访问此URL进行授权
        
        Args:
            redirect_uri: 授权回调地址
            scope: 请求的权限范围，默认只请求基本用户信息权限
            state: 维护请求和回调状态的参数，用于防止CSRF攻击
            
        Returns:
            str: 完整的授权URL
        """
        if scope is None:
            scope = os.getenv("FEISHU_OAUTH_SCOPES", "").strip()
        if not scope:
            scope = " ".join(DEFAULT_FEISHU_OAUTH_SCOPES)
        scope = " ".join(dict.fromkeys(scope.split()))

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
        
        query_string = urlencode(params)
        full_url = f"{authorize_url}?{query_string}"
        logger.info("生成授权URL成功")
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
            response = requests.post(
                token_url,
                json=data,
                timeout=FEISHU_REQUEST_TIMEOUT_SECONDS,
            )
            response_data = response.json()
            
            # 检查请求是否成功
            if response.status_code != 200 or response_data.get("code") != 0:
                error_msg = (
                    response_data.get("error_description")
                    or response_data.get("msg")
                    or response_data.get("error")
                    or "未知错误"
                )
                error_code = response_data.get("code") or response_data.get("error")
                logger.error(f"获取用户访问令牌失败: {error_msg}")
                raise FeishuAPIError(f"获取用户访问令牌失败: {error_msg}", error_code)
            
            # 保存令牌信息
            token_data = response_data.get("data", response_data)
            token_info = self._normalize_token_info(token_data)
            if not token_info.get("access_token"):
                raise FeishuAPIError("获取用户访问令牌失败: 响应中缺少 access_token")
            
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
        if not self.app_secret:
            raise ValueError("刷新令牌需要配置 FEISHU_APP_SECRET")
        
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
            response = requests.post(
                refresh_url,
                json=data,
                timeout=FEISHU_REQUEST_TIMEOUT_SECONDS,
            )
            response_data = response.json()
            
            # 检查请求是否成功
            if response.status_code != 200 or response_data.get("code") != 0:
                error_msg = (
                    response_data.get("error_description")
                    or response_data.get("msg")
                    or response_data.get("error")
                    or "未知错误"
                )
                error_code = response_data.get("code") or response_data.get("error")
                logger.error(f"刷新用户访问令牌失败: {error_msg}")
                raise FeishuAPIError(f"刷新用户访问令牌失败: {error_msg}", error_code)
            
            # 保存新的令牌信息
            token_data = response_data.get("data", response_data)
            new_token_info = self._normalize_token_info(token_data)
            if not new_token_info.get("access_token"):
                raise FeishuAPIError("刷新用户访问令牌失败: 响应中缺少 access_token")
            if not new_token_info.get("refresh_token"):
                raise FeishuAPIError("刷新用户访问令牌失败: 响应中缺少新的 refresh_token")
            
            logger.info("刷新用户访问令牌成功")
            return new_token_info
            
        except Exception as e:
            logger.error(f"刷新用户访问令牌时发生异常: {str(e)}")
            raise
    
    @staticmethod
    def _normalize_token_info(token_data):
        """Normalize v2 OAuth fields and record absolute expiry timestamps."""
        timestamp = int(time.time())
        expires_in = int(token_data.get("expires_in") or 0)
        refresh_expires_in = int(
            token_data.get("refresh_token_expires_in")
            or token_data.get("refresh_expires_in")
            or 0
        )
        token_info = {
            "access_token": token_data.get("access_token"),
            "expires_in": expires_in,
            "refresh_token": token_data.get("refresh_token"),
            "refresh_token_expires_in": refresh_expires_in,
            "token_type": token_data.get("token_type", "Bearer"),
            "scope": token_data.get("scope") or "",
            "timestamp": timestamp,
            "expires_at": timestamp + expires_in if expires_in else 0,
            "refresh_token_expires_at": (
                timestamp + refresh_expires_in if refresh_expires_in else 0
            ),
        }
        return token_info

    def save_token_to_file(self, token_info, file_path=None):
        """
        将令牌信息保存到文件
        
        Args:
            token_info: 令牌信息字典
            file_path: 保存文件路径，默认为data/user_token.json
        """
        # 设置默认文件路径
        if file_path is None:
            file_path = _default_token_path()

        absolute_path = os.path.abspath(file_path)
        data_dir = os.path.dirname(absolute_path)
        os.makedirs(data_dir, exist_ok=True)
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=data_dir,
                prefix=".feishu-token-",
                suffix=".tmp",
                delete=False,
            ) as f:
                temp_path = f.name
                json.dump(token_info, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            try:
                os.chmod(temp_path, 0o600)
            except OSError:
                pass
            os.replace(temp_path, absolute_path)
            logger.info(f"令牌信息已保存到 {absolute_path}")
        except Exception as e:
            logger.error(f"保存令牌信息到文件时发生异常: {str(e)}")
            raise
        finally:
            if temp_path and os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
    
    def load_token_from_file(self, file_path=None):
        """
        从文件加载令牌信息
        
        Args:
            file_path: 文件路径，默认为data/user_token.json
            
        Returns:
            dict: 令牌信息字典，如果文件不存在或加载失败则返回None
        """
        # 设置默认文件路径
        if file_path is None:
            file_path = _default_token_path()
        
        try:
            if not os.path.exists(file_path):
                logger.warning(f"令牌文件 {file_path} 不存在")
                return None
            
            with open(file_path, "r", encoding="utf-8") as f:
                token_info = json.load(f)

            if (
                "refresh_token_expires_in" not in token_info
                and "refresh_expires_in" in token_info
            ):
                token_info["refresh_token_expires_in"] = token_info.get(
                    "refresh_expires_in", 0
                )
            
            logger.info(f"从 {file_path} 加载令牌信息成功")
            return token_info
            
        except Exception as e:
            logger.error(f"从文件加载令牌信息时发生异常: {str(e)}")
            return None
    
    def is_token_expired(self, token_info, buffer_seconds=TOKEN_EXPIRY_BUFFER_SECONDS):
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

    def is_refresh_token_expired(
        self,
        token_info,
        buffer_seconds=TOKEN_EXPIRY_BUFFER_SECONDS,
    ):
        if not token_info or not token_info.get("refresh_token"):
            return True
        timestamp = int(token_info.get("timestamp") or 0)
        expires_in = int(
            token_info.get("refresh_token_expires_in")
            or token_info.get("refresh_expires_in")
            or 0
        )
        if not expires_in:
            return False
        return (int(time.time()) - timestamp) + buffer_seconds >= expires_in

    def ensure_valid_token(self, file_path=None):
        """Load and rotate a user token once across threads and worker processes."""
        file_path = file_path or _default_token_path()
        with _token_file_lock(file_path):
            with _interprocess_token_file_lock(file_path):
                # Reload only after acquiring both locks. Another worker may have
                # rotated the one-time refresh token while this worker waited.
                token_info = self.load_token_from_file(file_path)
                if not token_info or not token_info.get("access_token"):
                    raise FeishuAuthorizationRequired("尚未完成飞书授权")
                if not self.is_token_expired(token_info):
                    return token_info, "connected"
                if self.is_refresh_token_expired(token_info):
                    raise FeishuAuthorizationRequired("飞书授权已失效，需要重新授权")

                refreshed_token = self.refresh_user_access_token(
                    token_info.get("refresh_token")
                )
                self.save_token_to_file(refreshed_token, file_path)
                return refreshed_token, "refreshed"

    def get_connection_status(self, file_path=None, refresh=True):
        """Return connection metadata without exposing either OAuth token."""
        file_path = file_path or _default_token_path()
        token_info = self.load_token_from_file(file_path)
        if not token_info or not token_info.get("access_token"):
            return {
                "valid": False,
                "connected": False,
                "status": "missing",
                "needs_reauthorization": False,
                "refreshable": False,
            }

        state = "connected"
        if self.is_token_expired(token_info):
            if not refresh:
                state = "expired"
            else:
                try:
                    token_info, state = self.ensure_valid_token(file_path)
                except FeishuAuthorizationRequired as exc:
                    return {
                        "valid": False,
                        "connected": True,
                        "status": "reauthorization_required",
                        "needs_reauthorization": True,
                        "refreshable": False,
                        "message": str(exc),
                    }
                except FeishuAPIError as exc:
                    needs_reauthorization = exc.code in REAUTHORIZE_ERROR_CODES
                    configuration_error = (
                        exc.code in REFRESH_CONFIGURATION_ERROR_CODES
                    )
                    return {
                        "valid": False,
                        "connected": True,
                        "status": (
                            "reauthorization_required"
                            if needs_reauthorization
                            else (
                                "refresh_configuration_error"
                                if configuration_error
                                else "refresh_failed"
                            )
                        ),
                        "needs_reauthorization": (
                            needs_reauthorization or exc.code == 20024
                        ),
                        "refreshable": (
                            not needs_reauthorization and exc.code != 20024
                        ),
                        "provider_error_code": exc.code,
                        "message": str(exc),
                    }
                except Exception as exc:
                    logger.warning("自动刷新飞书 token 失败: %s", exc)
                    return {
                        "valid": False,
                        "connected": True,
                        "status": "refresh_failed",
                        "needs_reauthorization": False,
                        "refreshable": True,
                        "message": "暂时无法自动刷新，请稍后重试",
                    }

        timestamp = int(token_info.get("timestamp") or 0)
        expires_in = int(token_info.get("expires_in") or 0)
        expires_at = int(
            token_info.get("expires_at")
            or (timestamp + expires_in if timestamp and expires_in else 0)
        )
        return {
            "valid": state in {"connected", "refreshed"},
            "connected": True,
            "status": state,
            "needs_reauthorization": False,
            "refreshable": bool(token_info.get("refresh_token")),
            "authorized_at": timestamp or None,
            "expires_at": expires_at or None,
            "scope": token_info.get("scope") or "",
        }
    
    def get_auth_url(self):
        """兼容旧调用：生成授权页 URL。"""
        return self.generate_authorize_url()

    def get_user_access_token_by_code(self, code):
        """兼容旧调用：通过授权码换取 Token。"""
        return self.get_user_access_token(code)


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
        print("\n成功获取用户访问令牌")
        print(f"访问令牌: {token_info['access_token']}")
        print(f"令牌有效期: {token_info['expires_in']} 秒")
        if token_info.get('refresh_token'):
            print(f"刷新令牌: {token_info['refresh_token']}")
        print(f"权限范围: {token_info['scope']}")
        
        # 询问是否保存令牌信息
        save_token = input("\n是否将令牌信息保存到文件？(y/n，默认为y): ").strip().lower()
        if save_token != 'n':
            file_path = input(f"请输入保存文件路径（默认: {_default_token_path()}）: ").strip()
            file_path = file_path or _default_token_path()
            feishu_api.save_token_to_file(token_info, file_path)
            print(f"令牌信息已保存到: {file_path}")
        
        # 返回获取到的令牌信息
        return token_info
        
    except Exception as e:
        logger.error(f"获取用户访问令牌过程中发生错误: {str(e)}")
        print(f"错误: {str(e)}")
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
            file_path = input(f"请输入令牌文件路径（默认: {_default_token_path()}）: ").strip()
            file_path = file_path or _default_token_path()
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
        print("\n成功刷新用户访问令牌")
        print(f"新的访问令牌: {new_token_info['access_token']}")
        print(f"令牌有效期: {new_token_info['expires_in']} 秒")
        print(f"新的刷新令牌: {new_token_info['refresh_token']}")
        print(f"权限范围: {new_token_info['scope']}")
        
        # 询问是否保存新的令牌信息
        save_token = input("\n是否将新的令牌信息保存到文件？(y/n，默认为y): ").strip().lower()
        if save_token != 'n':
            file_path = input(f"请输入保存文件路径（默认: {_default_token_path()}）: ").strip()
            file_path = file_path or _default_token_path()
            feishu_api.save_token_to_file(new_token_info, file_path)
            print(f"新的令牌信息已保存到: {file_path}")
        
        # 返回刷新后的令牌信息
        return new_token_info
        
    except Exception as e:
        logger.error(f"刷新用户访问令牌过程中发生错误: {str(e)}")
        print(f"错误: {str(e)}")
        return None


def get_user_access_token(interactive=True, file_path=None):
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
        
        try:
            token_info, state = feishu_api.ensure_valid_token(file_path)
            logger.info(
                "使用%s缓存令牌",
                "自动刷新后的" if state == "refreshed" else "有效的",
            )
            return token_info
        except Exception as e:
            logger.warning(f"未找到可复用的用户令牌: {str(e)}")
        
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
