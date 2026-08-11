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
