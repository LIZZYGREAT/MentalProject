from pathlib import Path

import pytest

from app.config import Settings


def valid_environment() -> dict[str, str]:
    return {
        "APP_ENV": "test",
        "FEISHU_APP_ID": "app",
        "FEISHU_APP_SECRET": "secret",
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


def test_legacy_feishu_app_config_still_works():
    settings = Settings.from_env(
        valid_environment(), base_dir=Path(__file__).resolve().parents[1]
    )

    assert settings.feishu_bot_app_id == "app"
    assert settings.feishu_calendar_app_id == "app"


@pytest.mark.parametrize(
    "name,value",
    [
        ("FEISHU_BOT_APP_ID", "bot-app"),
        ("FEISHU_BOT_APP_SECRET", "bot-secret"),
    ],
)
def test_partial_bot_credentials_are_rejected_even_with_legacy_pair(name, value):
    environment = valid_environment()
    environment[name] = value

    with pytest.raises(ValueError, match="FEISHU_BOT_APP_ID.*configured together"):
        Settings.from_env(
            environment, base_dir=Path(__file__).resolve().parents[1]
        )


def test_explicit_bot_pair_does_not_mix_with_legacy_pair():
    environment = valid_environment()
    environment.update(
        {"FEISHU_BOT_APP_ID": "bot-app", "FEISHU_BOT_APP_SECRET": "bot-secret"}
    )

    settings = Settings.from_env(
        environment, base_dir=Path(__file__).resolve().parents[1]
    )

    assert settings.feishu_bot_app_id == "bot-app"
    assert settings.feishu_bot_app_secret == "bot-secret"


def test_partial_legacy_feishu_pair_is_rejected():
    environment = valid_environment()
    environment.pop("FEISHU_APP_SECRET")

    with pytest.raises(ValueError, match="FEISHU_BOT_APP_SECRET"):
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
