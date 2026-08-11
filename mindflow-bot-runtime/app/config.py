"""Environment-only configuration for the production runtime."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Mapping, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _int(env: Mapping[str, str], name: str, default: int, minimum: int = 1) -> int:
    try:
        value = int(env.get(name, str(default)))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


@dataclass(frozen=True)
class Settings:
    app_env: str
    log_level: str
    feishu_app_id: str
    feishu_app_secret: str
    deepseek_api_key: str
    deepseek_base_url: str
    deepseek_model: str
    database_url: str
    token_encryption_key: str
    care_skill_path: Path
    timezone_name: str = "Asia/Shanghai"
    queue_max_size: int = 100
    agent_workers: int = 2
    history_limit: int = 16
    max_tool_steps: int = 4
    agent_timeout_seconds: int = 30
    max_retries: int = 1

    @classmethod
    def from_env(
        cls,
        env: Optional[Mapping[str, str]] = None,
        *,
        base_dir: Optional[Path] = None,
        validate: bool = True,
    ) -> "Settings":
        root = base_dir or Path.cwd()
        if env is None:
            from dotenv import load_dotenv

            env_path = root / ".env"
            if not env_path.is_file() and (root.parent / ".env").is_file():
                env_path = root.parent / ".env"
            load_dotenv(env_path, override=False)
            values: Mapping[str, str] = os.environ
        else:
            values = env
        skill_value = values.get(
            "CARE_SKILL_PATH", "skills/mental-health-care/SKILL.md"
        )
        skill_path = Path(skill_value)
        if not skill_path.is_absolute():
            skill_path = (root / skill_path).resolve()
        settings = cls(
            app_env=values.get("APP_ENV", "production").strip().lower(),
            log_level=values.get("LOG_LEVEL", "INFO").strip().upper(),
            feishu_app_id=values.get("FEISHU_APP_ID", "").strip(),
            feishu_app_secret=values.get("FEISHU_APP_SECRET", "").strip(),
            deepseek_api_key=values.get("DEEPSEEK_API_KEY", "").strip(),
            deepseek_base_url=values.get(
                "DEEPSEEK_BASE_URL", "https://api.deepseek.com"
            ).strip().rstrip("/"),
            deepseek_model=values.get("DEEPSEEK_MODEL", "deepseek-v4-flash").strip(),
            database_url=values.get("DATABASE_URL", "").strip(),
            token_encryption_key=values.get("TOKEN_ENCRYPTION_KEY", "").strip(),
            care_skill_path=skill_path,
            timezone_name=values.get("APP_TIMEZONE", "Asia/Shanghai").strip(),
            queue_max_size=_int(values, "BOT_QUEUE_MAX_SIZE", 100),
            agent_workers=_int(values, "BOT_AGENT_WORKERS", 2),
            history_limit=_int(values, "BOT_HISTORY_LIMIT", 16),
            max_tool_steps=_int(values, "AGENT_MAX_TOOL_STEPS", 4),
            agent_timeout_seconds=_int(values, "AGENT_TIMEOUT_SECONDS", 30),
            max_retries=_int(values, "AGENT_MAX_RETRIES", 1, minimum=0),
        )
        if validate:
            settings.validate()
        return settings

    def validate(self) -> None:
        required = {
            "FEISHU_APP_ID": self.feishu_app_id,
            "FEISHU_APP_SECRET": self.feishu_app_secret,
            "DEEPSEEK_API_KEY": self.deepseek_api_key,
            "DATABASE_URL": self.database_url,
            "TOKEN_ENCRYPTION_KEY": self.token_encryption_key,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ValueError("Missing required environment values: " + ", ".join(missing))
        if self.app_env == "production" and not self.database_url.startswith(
            ("postgresql://", "postgresql+psycopg://")
        ):
            raise ValueError("Production DATABASE_URL must use PostgreSQL")
        if not self.care_skill_path.is_file():
            raise ValueError(f"CARE_SKILL_PATH does not exist: {self.care_skill_path}")
        try:
            ZoneInfo(self.timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"APP_TIMEZONE is invalid: {self.timezone_name}") from exc
