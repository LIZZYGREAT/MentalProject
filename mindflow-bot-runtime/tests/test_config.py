from pathlib import Path

import pytest
import yaml
from starlette.testclient import TestClient

from app.admin_web.auth import hash_password
from app.admin_web.main import create_app
from app.config import Settings
from helpers import memory_database


def valid_environment() -> dict[str, str]:
    return {
        "APP_ENV": "test",
        "FEISHU_BOT_APP_ID": "app",
        "FEISHU_BOT_APP_SECRET": "secret",
        "DEEPSEEK_API_KEY": "key",
        "DATABASE_URL": "sqlite:///:memory:",
        "TOKEN_ENCRYPTION_KEY": "test-key",
        "CLAUDE_MODEL": "deepseek-primary",
        "CLAUDE_DEFAULT_OPUS_MODEL": "deepseek-v4-pro",
        "CLAUDE_DEFAULT_SONNET_MODEL": "deepseek-v4-pro",
        "CLAUDE_DEFAULT_HAIKU_MODEL": "deepseek-v4-flash",
        "CLAUDE_CODE_SUBAGENT_MODEL": "deepseek-v4-flash",
    }


def test_all_claude_model_roles_are_loaded_explicitly():
    settings = Settings.from_env(
        valid_environment(), base_dir=Path(__file__).resolve().parents[1]
    )

    assert settings.claude_model == "deepseek-primary"
    assert settings.claude_default_opus_model == "deepseek-v4-pro"
    assert settings.claude_default_sonnet_model == "deepseek-v4-pro"
    assert settings.claude_default_haiku_model == "deepseek-v4-flash"
    assert settings.claude_code_subagent_model == "deepseek-v4-flash"


def test_profile_calibration_is_disabled_by_default_and_requires_opt_in():
    base = valid_environment()
    settings = Settings.from_env(base, base_dir=Path(__file__).resolve().parents[1])
    assert settings.profile_calibration_enabled is False
    base["PROFILE_CALIBRATION_ENABLED"] = "true"
    enabled = Settings.from_env(base, base_dir=Path(__file__).resolve().parents[1])
    assert enabled.profile_calibration_enabled is True


def test_daily_review_is_disabled_by_default_and_ws_transport_enables_it():
    environment = valid_environment()
    settings = Settings.from_env(
        environment, base_dir=Path(__file__).resolve().parents[1]
    )
    assert settings.daily_review_enabled is False
    assert settings.feishu_card_action_transport == "ws"

    environment["DAILY_REVIEW_ENABLED"] = "true"
    enabled = Settings.from_env(
        environment, base_dir=Path(__file__).resolve().parents[1]
    )
    assert enabled.daily_review_enabled is True

    environment.update(
        {
            "FEISHU_CARD_ACTION_TRANSPORT": "http",
            "FEISHU_CARD_CALLBACK_ENABLED": "true",
            "FEISHU_CARD_VERIFICATION_TOKEN": "verification-token",
            "FEISHU_CARD_ENCRYPT_KEY": "encrypt-key",
        }
    )
    enabled = Settings.from_env(
        environment, base_dir=Path(__file__).resolve().parents[1]
    )
    assert enabled.daily_review_enabled is True


def test_response_ux_defaults_and_presentation_model_fallback():
    settings = Settings.from_env(
        valid_environment(), base_dir=Path(__file__).resolve().parents[1]
    )

    assert settings.generic_progress_delay_seconds == 10
    assert settings.tool_progress_grace_seconds == 1.2
    assert settings.progress_cooldown_seconds == 3
    assert settings.progress_max_messages == 1
    assert settings.response_segmentation_enabled is True
    assert settings.response_segment_min_total_chars == 320
    assert settings.response_segment_target_chars == 260
    assert settings.response_segment_max_chars == 650
    assert settings.response_max_segments == 3
    assert settings.presentation_agent_min_chars == 600
    assert settings.presentation_agent_timeout_seconds == 4
    assert settings.presentation_model == "deepseek-v4-flash"
    assert settings.claude_partial_messages_enabled is False


def test_progress_timers_have_independent_configuration():
    environment = valid_environment()
    environment.update(
        {
            "GENERIC_PROGRESS_DELAY_SECONDS": "8.5",
            "TOOL_PROGRESS_GRACE_SECONDS": "0.75",
        }
    )

    settings = Settings.from_env(
        environment, base_dir=Path(__file__).resolve().parents[1]
    )

    assert settings.generic_progress_delay_seconds == 8.5
    assert settings.tool_progress_grace_seconds == 0.75


def test_vision_schedule_item_cap_cannot_exceed_visible_preview_limit():
    environment = valid_environment()
    environment["VISION_SCHEDULE_MAX_ITEMS"] = "21"
    with pytest.raises(ValueError, match="VISION_SCHEDULE_MAX_ITEMS.*20"):
        Settings.from_env(
            environment, base_dir=Path(__file__).resolve().parents[1]
        )

    environment["VISION_SCHEDULE_MAX_ITEMS"] = "20"
    environment["VISION_SCHEDULE_MAX_CALENDAR_WRITES"] = "401"
    settings = Settings.from_env(
        environment, base_dir=Path(__file__).resolve().parents[1]
    )
    assert settings.vision_schedule_max_items == 20
    assert settings.vision_schedule_max_calendar_writes == 401


def test_haiku_and_subagent_must_use_the_same_model():
    environment = valid_environment()
    environment["CLAUDE_CODE_SUBAGENT_MODEL"] = "wrong-model"

    with pytest.raises(ValueError, match="must match"):
        Settings.from_env(
            environment, base_dir=Path(__file__).resolve().parents[1]
        )


def test_explicit_dual_feishu_app_config():
    environment = valid_environment()
    environment.update(
        {
            "FEISHU_BOT_APP_ID": "bot-app",
            "FEISHU_BOT_APP_SECRET": "bot-secret",
            "FEISHU_CALENDAR_APP_ID": "calendar-app",
            "FEISHU_CALENDAR_APP_SECRET": "calendar-secret",
        }
    )
    settings = Settings.from_env(
        environment, base_dir=Path(__file__).resolve().parents[1]
    )

    assert settings.feishu_bot_app_id == "bot-app"
    assert settings.feishu_bot_app_secret == "bot-secret"
    assert settings.feishu_calendar_app_id == "calendar-app"
    assert settings.feishu_calendar_app_secret == "calendar-secret"


def test_calendar_app_falls_back_to_bot_app():
    environment = valid_environment()
    environment.update(
        {"FEISHU_BOT_APP_ID": "bot-app", "FEISHU_BOT_APP_SECRET": "bot-secret"}
    )
    settings = Settings.from_env(
        environment, base_dir=Path(__file__).resolve().parents[1]
    )

    assert settings.feishu_calendar_app_id == "bot-app"
    assert settings.feishu_calendar_app_secret == "bot-secret"


@pytest.mark.parametrize(
    "name,value",
    [
        ("FEISHU_BOT_APP_ID", "bot-app"),
        ("FEISHU_BOT_APP_SECRET", "bot-secret"),
    ],
)
def test_partial_bot_credentials_are_rejected(name, value):
    environment = valid_environment()
    environment.pop("FEISHU_BOT_APP_ID")
    environment.pop("FEISHU_BOT_APP_SECRET")
    environment[name] = value

    with pytest.raises(ValueError, match="FEISHU_BOT_APP_ID.*configured together"):
        Settings.from_env(
            environment, base_dir=Path(__file__).resolve().parents[1]
        )


def test_legacy_feishu_pair_is_not_a_runtime_fallback():
    environment = valid_environment()
    environment.pop("FEISHU_BOT_APP_ID")
    environment.pop("FEISHU_BOT_APP_SECRET")
    environment.update({"FEISHU_APP_ID": "legacy", "FEISHU_APP_SECRET": "legacy"})

    with pytest.raises(ValueError, match="FEISHU_BOT_APP_ID"):
        Settings.from_env(
            environment, base_dir=Path(__file__).resolve().parents[1]
        )


@pytest.mark.parametrize(
    "name,value",
    [
        ("FEISHU_CALENDAR_APP_ID", "calendar-app"),
        ("FEISHU_CALENDAR_APP_SECRET", "calendar-secret"),
    ],
)
def test_partial_calendar_credentials_are_rejected(name, value):
    environment = valid_environment()
    environment[name] = value

    with pytest.raises(ValueError, match="must be configured together"):
        Settings.from_env(
            environment, base_dir=Path(__file__).resolve().parents[1]
        )


def test_card_callback_requires_verified_encrypted_configuration_when_enabled():
    environment = valid_environment()
    environment["FEISHU_CARD_ACTION_TRANSPORT"] = "http"
    environment["FEISHU_CARD_CALLBACK_ENABLED"] = "true"

    with pytest.raises(ValueError, match="FEISHU_CARD_VERIFICATION_TOKEN"):
        Settings.from_env(
            environment, base_dir=Path(__file__).resolve().parents[1]
        )

    environment.update(
        {
            "FEISHU_CARD_VERIFICATION_TOKEN": "verification-token",
            "FEISHU_CARD_ENCRYPT_KEY": "encrypt-key",
            "FEISHU_CARD_CALLBACK_PORT": "8123",
        }
    )
    settings = Settings.from_env(
        environment, base_dir=Path(__file__).resolve().parents[1]
    )
    assert settings.feishu_card_callback_enabled is True
    assert settings.feishu_card_callback_port == 8123
    assert settings.feishu_card_callback_path == "/feishu/card/callback"


def test_ws_and_http_card_action_ingress_cannot_be_enabled_together():
    environment = valid_environment()
    environment["FEISHU_CARD_CALLBACK_ENABLED"] = "true"

    with pytest.raises(ValueError, match="must be false"):
        Settings.from_env(
            environment, base_dir=Path(__file__).resolve().parents[1]
        )


def test_card_action_transport_rejects_unknown_mode():
    environment = valid_environment()
    environment["FEISHU_CARD_ACTION_TRANSPORT"] = "auto"

    with pytest.raises(ValueError, match="must be ws or http"):
        Settings.from_env(
            environment, base_dir=Path(__file__).resolve().parents[1]
        )


def test_bot_validation_is_not_coupled_to_admin_credentials():
    environment = valid_environment()
    environment["ADMIN_ENABLED"] = "true"

    settings = Settings.from_env(
        environment, base_dir=Path(__file__).resolve().parents[1]
    )

    assert settings.admin_enabled is True
    with pytest.raises(ValueError, match="ADMIN_USERNAME"):
        settings.validate_admin()


def test_presentation_agent_performance_policy_defaults_and_validation():
    environment = valid_environment()
    settings = Settings.from_env(
        environment, base_dir=Path(__file__).resolve().parents[1]
    )
    assert settings.presentation_agent_mode == "adaptive"
    assert settings.presentation_agent_timeout_seconds == 4.0
    assert settings.presentation_agent_disconnect_timeout_seconds == 0.5
    assert settings.presentation_agent_max_pending_cleanups == 1

    environment["PRESENTATION_AGENT_MODE"] = "invalid"
    with pytest.raises(ValueError, match="PRESENTATION_AGENT_MODE"):
        Settings.from_env(
            environment, base_dir=Path(__file__).resolve().parents[1]
        )


def test_legacy_presentation_enabled_maps_to_mode_and_warns_once(caplog, monkeypatch):
    import app.config as config_module

    monkeypatch.setattr(config_module, "_legacy_presentation_warning_emitted", False)
    environment = valid_environment()
    environment["PRESENTATION_AGENT_ENABLED"] = "false"
    with caplog.at_level("WARNING"):
        disabled = Settings.from_env(
            environment, base_dir=Path(__file__).resolve().parents[1]
        )
        Settings.from_env(environment, base_dir=Path(__file__).resolve().parents[1])
    assert disabled.presentation_agent_mode == "off"
    assert sum("PRESENTATION_AGENT_ENABLED is deprecated" in item for item in caplog.messages) == 1

    environment["PRESENTATION_AGENT_ENABLED"] = "true"
    enabled = Settings.from_env(
        environment, base_dir=Path(__file__).resolve().parents[1]
    )
    assert enabled.presentation_agent_mode == "adaptive"


def test_explicit_presentation_mode_is_authoritative_over_legacy_flag():
    environment = valid_environment()
    environment.update(
        {"PRESENTATION_AGENT_MODE": "always", "PRESENTATION_AGENT_ENABLED": "false"}
    )
    settings = Settings.from_env(
        environment, base_dir=Path(__file__).resolve().parents[1]
    )
    assert settings.presentation_agent_mode == "always"


def test_admin_validation_fails_closed_when_disabled_or_misconfigured():
    environment = valid_environment()
    environment.update(
        {
            "ADMIN_ENABLED": "false",
            "ADMIN_USERNAME": "admin",
            "ADMIN_PASSWORD_HASH": "hash",
            "ADMIN_SESSION_SECRET": "long-enough-session-secret",
        }
    )
    disabled = Settings.from_env(
        environment, base_dir=Path(__file__).resolve().parents[1]
    )
    with pytest.raises(ValueError, match="ADMIN_ENABLED"):
        disabled.validate_admin()

    environment["ADMIN_ENABLED"] = "true"
    environment["ADMIN_PORT"] = "70000"
    bad_port = Settings.from_env(
        environment, base_dir=Path(__file__).resolve().parents[1]
    )
    with pytest.raises(ValueError, match="ADMIN_PORT"):
        bad_port.validate_admin()


def test_compose_keeps_container_admin_port_stable():
    compose = (
        Path(__file__).resolve().parents[1] / "compose.yaml"
    ).read_text(encoding="utf-8")

    assert 'ADMIN_PORT: 8081' in compose
    assert '127.0.0.1:${ADMIN_HOST_PORT:-8081}:8081' in compose
    assert '127.0.0.1:${ADMIN_PORT:-8081}:8081' not in compose


def _normalized_volume(value):
    if isinstance(value, dict):
        return {
            "source": value.get("source"),
            "target": value.get("target"),
            "read_only": bool(value.get("read_only")),
        }
    source, target, *options = str(value).split(":")
    return {
        "source": source,
        "target": target,
        "read_only": "ro" in options,
    }


def test_admin_compose_mounts_real_claude_runtime_read_only_without_changing_postgres():
    compose_path = Path(__file__).resolve().parents[1] / "compose.yaml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))

    admin_volumes = [
        _normalized_volume(value)
        for value in compose["services"]["admin"].get("volumes", [])
    ]
    assert {
        "source": "../claude-runtime",
        "target": "/srv/claude-runtime",
        "read_only": True,
    } in admin_volumes

    postgres_volumes = [
        _normalized_volume(value)
        for value in compose["services"]["postgres"].get("volumes", [])
    ]
    assert any(
        volume["source"] == "postgres_data"
        and volume["target"] == "/var/lib/postgresql/data"
        for volume in postgres_volumes
    )


def test_admin_production_like_startup_uses_mounted_runtime_and_missing_mount_fails_closed(
    tmp_path,
):
    runtime_dir = Path(__file__).resolve().parents[2] / "claude-runtime"
    environment = valid_environment()
    environment.update(
        {
            "CLAUDE_WORKDIR": str(runtime_dir),
            "ADMIN_ENABLED": "true",
            "ADMIN_USERNAME": "admin",
            "ADMIN_PASSWORD_HASH": hash_password("correct-password"),
            "ADMIN_SESSION_SECRET": "a-long-test-session-secret",
        }
    )

    settings = Settings.from_env(
        environment, base_dir=Path(__file__).resolve().parents[1]
    )
    with TestClient(create_app(memory_database(), settings)) as client:
        assert client.get("/", follow_redirects=False).status_code in {302, 307}

    environment["CLAUDE_WORKDIR"] = str(tmp_path / "missing-claude-runtime")
    with pytest.raises(ValueError, match="CLAUDE_WORKDIR does not exist"):
        Settings.from_env(
            environment, base_dir=Path(__file__).resolve().parents[1]
        )
