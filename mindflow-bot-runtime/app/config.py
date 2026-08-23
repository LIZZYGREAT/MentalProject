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


def _float(
    env: Mapping[str, str], name: str, default: float, minimum: float = 0.0
) -> float:
    try:
        value = float(env.get(name, str(default)))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number") from exc
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def _bool(env: Mapping[str, str], name: str, default: bool = False) -> bool:
    value = env.get(name, "true" if default else "false").strip().lower()
    if value not in {"1", "0", "true", "false", "yes", "no", "on", "off"}:
        raise ValueError(f"{name} must be a boolean")
    return value in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    app_env: str
    log_level: str
    feishu_bot_app_id: str
    feishu_bot_app_secret: str
    feishu_calendar_app_id: str
    feishu_calendar_app_secret: str
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
    feishu_card_callback_enabled: bool = False
    feishu_card_callback_host: str = "0.0.0.0"
    feishu_card_callback_port: int = 8000
    feishu_card_callback_path: str = "/feishu/card/callback"
    feishu_card_verification_token: str = ""
    feishu_card_encrypt_key: str = ""
    timezone_name: str = "Asia/Shanghai"
    queue_max_size: int = 100
    participant_input_queue_size: int = 20
    max_active_agent_sessions: int = 2
    agent_session_idle_seconds: int = 120
    claude_timeout_seconds: int = 90
    claude_max_turns: int = 8
    progress_delay_seconds: int = 3
    progress_cooldown_seconds: int = 3
    progress_max_messages: int = 2
    response_segmentation_enabled: bool = True
    response_segment_min_total_chars: int = 320
    response_segment_target_chars: int = 260
    response_segment_max_chars: int = 650
    response_max_segments: int = 3
    presentation_agent_enabled: bool = True
    presentation_agent_min_chars: int = 600
    presentation_agent_timeout_seconds: float = 4.0
    presentation_agent_max_segments: int = 3
    presentation_model: str = ""
    claude_partial_messages_enabled: bool = False
    feishu_send_max_retries: int = 1
    feishu_gateway_start_timeout_seconds: int = 30
    feishu_gateway_stop_timeout_seconds: int = 8
    feishu_gateway_device_flow_close_timeout_seconds: int = 8
    forecast_daily_prepare_local_time: str = "07:30"
    forecast_calendar_sync_interval_seconds: int = 300
    semantic_api_enabled: bool = False
    semantic_api_url: str = "https://api.deepseek.com/chat/completions"
    semantic_api_model: str = "deepseek-v4-flash"
    semantic_api_timeout_seconds: float = 8.0
    semantic_max_concurrency: int = 2
    semantic_materiality_threshold: float = 0.03
    forecast_max_concurrency: int = 1
    warning_poll_interval_seconds: int = 15
    warning_lead_minutes: int = 20
    warning_late_grace_minutes: int = 10
    warning_max_attempts: int = 5
    warning_retry_base_seconds: int = 60
    warning_claim_lease_seconds: int = 120
    warning_episode_drift_minutes: int = 15
    warning_max_daily_sends: int = 2
    warning_min_interval_minutes: int = 240
    profile_calibration_enabled: bool = False
    admin_enabled: bool = True
    admin_host: str = "0.0.0.0"
    admin_port: int = 8081
    admin_username: str = ""
    admin_password_hash: str = ""
    admin_session_secret: str = ""
    admin_session_ttl_seconds: int = 28800
    admin_secure_cookie: bool = False

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

        legacy_app_id = values.get("FEISHU_APP_ID", "").strip()
        legacy_app_secret = values.get("FEISHU_APP_SECRET", "").strip()
        explicit_bot_app_id = values.get("FEISHU_BOT_APP_ID", "").strip()
        explicit_bot_app_secret = values.get("FEISHU_BOT_APP_SECRET", "").strip()
        if bool(explicit_bot_app_id) != bool(explicit_bot_app_secret):
            raise ValueError(
                "FEISHU_BOT_APP_ID and FEISHU_BOT_APP_SECRET "
                "must be configured together"
            )
        if explicit_bot_app_id:
            bot_app_id = explicit_bot_app_id
            bot_app_secret = explicit_bot_app_secret
        else:
            bot_app_id = legacy_app_id
            bot_app_secret = legacy_app_secret
        explicit_calendar_app_id = values.get("FEISHU_CALENDAR_APP_ID", "").strip()
        explicit_calendar_app_secret = values.get(
            "FEISHU_CALENDAR_APP_SECRET", ""
        ).strip()
        if bool(explicit_calendar_app_id) != bool(explicit_calendar_app_secret):
            raise ValueError(
                "FEISHU_CALENDAR_APP_ID and FEISHU_CALENDAR_APP_SECRET "
                "must be configured together"
            )

        settings = cls(
            app_env=values.get("APP_ENV", "production").strip().lower(),
            log_level=values.get("LOG_LEVEL", "INFO").strip().upper(),
            feishu_bot_app_id=bot_app_id,
            feishu_bot_app_secret=bot_app_secret,
            feishu_calendar_app_id=explicit_calendar_app_id or bot_app_id,
            feishu_calendar_app_secret=explicit_calendar_app_secret or bot_app_secret,
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
            feishu_card_callback_enabled=_bool(
                values, "FEISHU_CARD_CALLBACK_ENABLED", False
            ),
            feishu_card_callback_host=values.get(
                "FEISHU_CARD_CALLBACK_HOST", "0.0.0.0"
            ).strip(),
            feishu_card_callback_port=_int(
                values, "FEISHU_CARD_CALLBACK_PORT", 8000
            ),
            feishu_card_callback_path=values.get(
                "FEISHU_CARD_CALLBACK_PATH", "/feishu/card/callback"
            ).strip(),
            feishu_card_verification_token=values.get(
                "FEISHU_CARD_VERIFICATION_TOKEN", ""
            ).strip(),
            feishu_card_encrypt_key=values.get(
                "FEISHU_CARD_ENCRYPT_KEY", ""
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
            progress_delay_seconds=_int(values, "PROGRESS_DELAY_SECONDS", 3),
            progress_cooldown_seconds=_int(
                values, "PROGRESS_COOLDOWN_SECONDS", 3
            ),
            progress_max_messages=_int(values, "PROGRESS_MAX_MESSAGES", 2),
            response_segmentation_enabled=_bool(
                values, "RESPONSE_SEGMENTATION_ENABLED", True
            ),
            response_segment_min_total_chars=_int(
                values, "RESPONSE_SEGMENT_MIN_TOTAL_CHARS", 320
            ),
            response_segment_target_chars=_int(
                values, "RESPONSE_SEGMENT_TARGET_CHARS", 260
            ),
            response_segment_max_chars=_int(
                values, "RESPONSE_SEGMENT_MAX_CHARS", 650
            ),
            response_max_segments=_int(
                values, "RESPONSE_MAX_SEGMENTS", 3
            ),
            presentation_agent_enabled=_bool(
                values, "PRESENTATION_AGENT_ENABLED", True
            ),
            presentation_agent_min_chars=_int(
                values, "PRESENTATION_AGENT_MIN_CHARS", 600
            ),
            presentation_agent_timeout_seconds=_float(
                values, "PRESENTATION_AGENT_TIMEOUT_SECONDS", 4.0, minimum=0.1
            ),
            presentation_agent_max_segments=_int(
                values, "PRESENTATION_AGENT_MAX_SEGMENTS", 3
            ),
            presentation_model=(
                values.get("PRESENTATION_MODEL", "").strip()
                or values.get("CLAUDE_CODE_SUBAGENT_MODEL", "").strip()
            ),
            claude_partial_messages_enabled=_bool(
                values, "CLAUDE_PARTIAL_MESSAGES_ENABLED", False
            ),
            feishu_send_max_retries=_int(
                values, "FEISHU_SEND_MAX_RETRIES", 1, minimum=0
            ),
            feishu_gateway_start_timeout_seconds=_int(
                values, "FEISHU_GATEWAY_START_TIMEOUT_SECONDS", 30
            ),
            feishu_gateway_stop_timeout_seconds=_int(
                values, "FEISHU_GATEWAY_STOP_TIMEOUT_SECONDS", 8
            ),
            feishu_gateway_device_flow_close_timeout_seconds=_int(
                values,
                "FEISHU_GATEWAY_DEVICE_FLOW_CLOSE_TIMEOUT_SECONDS",
                8,
            ),
            forecast_daily_prepare_local_time=values.get(
                "FORECAST_DAILY_PREPARE_LOCAL_TIME", "07:30"
            ).strip(),
            forecast_calendar_sync_interval_seconds=_int(
                values, "FORECAST_CALENDAR_SYNC_INTERVAL_SECONDS", 300, minimum=60
            ),
            semantic_api_enabled=_bool(values, "SEMANTIC_API_ENABLED", False),
            semantic_api_url=values.get(
                "SEMANTIC_API_URL", "https://api.deepseek.com/chat/completions"
            ).strip(),
            semantic_api_model=values.get(
                "SEMANTIC_API_MODEL", "deepseek-v4-flash"
            ).strip(),
            semantic_api_timeout_seconds=_float(
                values, "SEMANTIC_API_TIMEOUT_SECONDS", 8.0, minimum=0.1
            ),
            semantic_max_concurrency=_int(values, "SEMANTIC_MAX_CONCURRENCY", 2),
            semantic_materiality_threshold=_float(
                values, "SEMANTIC_MATERIALITY_THRESHOLD", 0.03
            ),
            forecast_max_concurrency=_int(values, "FORECAST_MAX_CONCURRENCY", 1),
            warning_poll_interval_seconds=_int(
                values, "WARNING_POLL_INTERVAL_SECONDS", 15
            ),
            warning_lead_minutes=_int(values, "WARNING_LEAD_MINUTES", 20, minimum=0),
            warning_late_grace_minutes=_int(values, "WARNING_LATE_GRACE_MINUTES", 10, minimum=0),
            warning_max_attempts=_int(values, "WARNING_MAX_ATTEMPTS", 5),
            warning_retry_base_seconds=_int(values, "WARNING_RETRY_BASE_SECONDS", 60),
            warning_claim_lease_seconds=_int(values, "WARNING_CLAIM_LEASE_SECONDS", 120),
            warning_episode_drift_minutes=_int(values, "WARNING_EPISODE_DRIFT_MINUTES", 15),
            warning_max_daily_sends=_int(values, "WARNING_MAX_DAILY_SENDS", 2, minimum=0),
            warning_min_interval_minutes=_int(values, "WARNING_MIN_INTERVAL_MINUTES", 240, minimum=0),
            profile_calibration_enabled=_bool(values, "PROFILE_CALIBRATION_ENABLED", False),
            admin_enabled=_bool(values, "ADMIN_ENABLED", True),
            admin_host=values.get("ADMIN_HOST", "0.0.0.0").strip(),
            admin_port=_int(values, "ADMIN_PORT", 8081),
            admin_username=values.get("ADMIN_USERNAME", "").strip(),
            admin_password_hash=values.get("ADMIN_PASSWORD_HASH", "").strip(),
            admin_session_secret=values.get("ADMIN_SESSION_SECRET", "").strip(),
            admin_session_ttl_seconds=_int(
                values, "ADMIN_SESSION_TTL_SECONDS", 28800, minimum=300
            ),
            admin_secure_cookie=_bool(values, "ADMIN_SECURE_COOKIE", False),
        )
        if validate:
            settings.validate()
        return settings

    def validate(self) -> None:
        required = {
            "FEISHU_BOT_APP_ID": self.feishu_bot_app_id,
            "FEISHU_BOT_APP_SECRET": self.feishu_bot_app_secret,
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
        if self.feishu_card_callback_enabled:
            missing_callback = [
                name
                for name, value in {
                    "FEISHU_CARD_VERIFICATION_TOKEN": self.feishu_card_verification_token,
                    "FEISHU_CARD_ENCRYPT_KEY": self.feishu_card_encrypt_key,
                }.items()
                if not value
            ]
            if missing_callback:
                raise ValueError(
                    "Missing required card callback values: "
                    + ", ".join(missing_callback)
                )
            if not self.feishu_card_callback_path.startswith("/"):
                raise ValueError("FEISHU_CARD_CALLBACK_PATH must start with /")
            if self.feishu_card_callback_port > 65535:
                raise ValueError("FEISHU_CARD_CALLBACK_PORT must be <= 65535")
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
        try:
            hour, minute = (int(part) for part in self.forecast_daily_prepare_local_time.split(":"))
        except (TypeError, ValueError) as exc:
            raise ValueError("FORECAST_DAILY_PREPARE_LOCAL_TIME must be HH:MM") from exc
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ValueError("FORECAST_DAILY_PREPARE_LOCAL_TIME must be HH:MM")
        if self.semantic_materiality_threshold > 1.0:
            raise ValueError("SEMANTIC_MATERIALITY_THRESHOLD must be <= 1")
        if self.response_segment_target_chars > self.response_segment_max_chars:
            raise ValueError(
                "RESPONSE_SEGMENT_TARGET_CHARS must be <= RESPONSE_SEGMENT_MAX_CHARS"
            )
        if self.response_max_segments > 3:
            raise ValueError("RESPONSE_MAX_SEGMENTS must be <= 3")
        if self.presentation_agent_max_segments > 3:
            raise ValueError("PRESENTATION_AGENT_MAX_SEGMENTS must be <= 3")

    def validate_admin(self) -> None:
        """Fail closed for the independent Admin process only."""

        if not self.admin_enabled:
            raise ValueError("ADMIN_ENABLED must be true to start Admin")
        if self.admin_port > 65535:
            raise ValueError("ADMIN_PORT must be <= 65535")
        missing_admin = [
            name
            for name, value in {
                "ADMIN_USERNAME": self.admin_username,
                "ADMIN_PASSWORD_HASH": self.admin_password_hash,
                "ADMIN_SESSION_SECRET": self.admin_session_secret,
            }.items()
            if not value
        ]
        if missing_admin:
            raise ValueError(
                "Missing required admin environment values: "
                + ", ".join(missing_admin)
            )
        if len(self.admin_session_secret) < 16:
            raise ValueError("ADMIN_SESSION_SECRET must be at least 16 characters")
