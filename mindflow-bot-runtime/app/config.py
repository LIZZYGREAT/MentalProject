"""Environment-only configuration for the Claude Agent SDK runtime."""

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
    database_url: str
    token_encryption_key: str
    claude_workdir: Path
    claude_settings_path: Path
    claude_anthropic_base_url: str
    claude_model: str
    claude_default_opus_model: str
    claude_default_sonnet_model: str
    claude_default_haiku_model: str
    claude_code_subagent_model: str
    timezone_name: str = "Asia/Shanghai"
    queue_max_size: int = 100
    participant_input_queue_size: int = 20
    max_active_agent_sessions: int = 2
    agent_session_idle_seconds: int = 120
    claude_timeout_seconds: int = 90
    claude_max_turns: int = 8
    progress_delay_seconds: int = 6
    progress_cooldown_seconds: int = 8
    progress_max_messages: int = 2
    feishu_send_max_retries: int = 1
    feishu_gateway_start_timeout_seconds: int = 30
    feishu_gateway_stop_timeout_seconds: int = 8

    @property
    def care_skill_path(self) -> Path:
        return (
            self.claude_plugin_path
            / "skills"
            / "mental-health-care"
            / "SKILL.md"
        )

    @property
    def claude_plugin_path(self) -> Path:
        return self.claude_workdir / "plugins" / "mindflow-care"

    @classmethod
    def from_env(
        cls,
        env: Optional[Mapping[str, str]] = None,
        *,
        base_dir: Optional[Path] = None,
        validate: bool = True,
    ) -> "Settings":
        root = (base_dir or Path.cwd()).resolve()
        if env is None:
            from dotenv import load_dotenv

            env_path = root / ".env"
            if not env_path.is_file() and (root.parent / ".env").is_file():
                env_path = root.parent / ".env"
            load_dotenv(env_path, override=False)
            values: Mapping[str, str] = os.environ
        else:
            values = env

        default_workdir = root.parent / "claude-runtime"
        workdir = Path(values.get("CLAUDE_WORKDIR", str(default_workdir)))
        if not workdir.is_absolute():
            workdir = (root / workdir).resolve()
        settings_value = values.get(
            "CLAUDE_SETTINGS_PATH", str(workdir / ".claude" / "settings.json")
        )
        settings_path = Path(settings_value)
        if not settings_path.is_absolute():
            settings_path = (root / settings_path).resolve()

        settings = cls(
            app_env=values.get("APP_ENV", "production").strip().lower(),
            log_level=values.get("LOG_LEVEL", "INFO").strip().upper(),
            feishu_app_id=values.get("FEISHU_APP_ID", "").strip(),
            feishu_app_secret=values.get("FEISHU_APP_SECRET", "").strip(),
            deepseek_api_key=values.get("DEEPSEEK_API_KEY", "").strip(),
            database_url=values.get("DATABASE_URL", "").strip(),
            token_encryption_key=values.get("TOKEN_ENCRYPTION_KEY", "").strip(),
            claude_workdir=workdir,
            claude_settings_path=settings_path,
            claude_anthropic_base_url=values.get(
                "CLAUDE_ANTHROPIC_BASE_URL", "https://api.deepseek.com/anthropic"
            ).strip().rstrip("/"),
            claude_model=values.get("CLAUDE_MODEL", "").strip(),
            claude_default_opus_model=values.get(
                "CLAUDE_DEFAULT_OPUS_MODEL", ""
            ).strip(),
            claude_default_sonnet_model=values.get(
                "CLAUDE_DEFAULT_SONNET_MODEL", ""
            ).strip(),
            claude_default_haiku_model=values.get(
                "CLAUDE_DEFAULT_HAIKU_MODEL", ""
            ).strip(),
            claude_code_subagent_model=values.get(
                "CLAUDE_CODE_SUBAGENT_MODEL", ""
            ).strip(),
            timezone_name=values.get("APP_TIMEZONE", "Asia/Shanghai").strip(),
            queue_max_size=_int(values, "BOT_QUEUE_MAX_SIZE", 100),
            participant_input_queue_size=_int(
                values, "PARTICIPANT_INPUT_QUEUE_SIZE", 20
            ),
            max_active_agent_sessions=_int(
                values, "MAX_ACTIVE_AGENT_SESSIONS", 2
            ),
            agent_session_idle_seconds=_int(
                values, "AGENT_SESSION_IDLE_SECONDS", 120
            ),
            claude_timeout_seconds=_int(values, "CLAUDE_TIMEOUT_SECONDS", 90),
            claude_max_turns=_int(values, "CLAUDE_MAX_TURNS", 8),
            progress_delay_seconds=_int(values, "PROGRESS_DELAY_SECONDS", 6),
            progress_cooldown_seconds=_int(
                values, "PROGRESS_COOLDOWN_SECONDS", 8
            ),
            progress_max_messages=_int(values, "PROGRESS_MAX_MESSAGES", 2),
            feishu_send_max_retries=_int(
                values, "FEISHU_SEND_MAX_RETRIES", 1, minimum=0
            ),
            feishu_gateway_start_timeout_seconds=_int(
                values, "FEISHU_GATEWAY_START_TIMEOUT_SECONDS", 30
            ),
            feishu_gateway_stop_timeout_seconds=_int(
                values, "FEISHU_GATEWAY_STOP_TIMEOUT_SECONDS", 8
            ),
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
            "CLAUDE_MODEL": self.claude_model,
            "CLAUDE_DEFAULT_OPUS_MODEL": self.claude_default_opus_model,
            "CLAUDE_DEFAULT_SONNET_MODEL": self.claude_default_sonnet_model,
            "CLAUDE_DEFAULT_HAIKU_MODEL": self.claude_default_haiku_model,
            "CLAUDE_CODE_SUBAGENT_MODEL": self.claude_code_subagent_model,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ValueError("Missing required environment values: " + ", ".join(missing))
        if self.claude_code_subagent_model != self.claude_default_haiku_model:
            raise ValueError(
                "CLAUDE_CODE_SUBAGENT_MODEL must match CLAUDE_DEFAULT_HAIKU_MODEL"
            )
        if self.app_env == "production" and not self.database_url.startswith(
            ("postgresql://", "postgresql+psycopg://")
        ):
            raise ValueError("Production DATABASE_URL must use PostgreSQL")
        if not self.claude_workdir.is_dir():
            raise ValueError(f"CLAUDE_WORKDIR does not exist: {self.claude_workdir}")
        if not self.claude_settings_path.is_file():
            raise ValueError(
                f"CLAUDE_SETTINGS_PATH does not exist: {self.claude_settings_path}"
            )
        if not self.care_skill_path.is_file():
            raise ValueError(f"Claude care Skill does not exist: {self.care_skill_path}")
        if not (
            self.claude_plugin_path / ".claude-plugin" / "plugin.json"
        ).is_file():
            raise ValueError(
                f"Claude care plugin manifest does not exist: {self.claude_plugin_path}"
            )
        try:
            ZoneInfo(self.timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"APP_TIMEZONE is invalid: {self.timezone_name}") from exc
