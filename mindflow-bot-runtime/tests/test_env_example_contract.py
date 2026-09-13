"""Keep deployment examples aligned with runtime capability settings."""

from pathlib import Path

from scripts import sync_env


RUNTIME_ROOT = Path(__file__).resolve().parents[1]


def _example_keys() -> set[str]:
    keys = set()
    for line in (RUNTIME_ROOT / ".env.example").read_text(
        encoding="utf-8-sig"
    ).splitlines():
        parsed = sync_env.parse_env_assignment(line)
        if parsed:
            keys.add(parsed["key"])
    return keys


def test_env_example_contains_critical_runtime_capabilities():
    required = {
        "SEMANTIC_API_ENABLED",
        "SEMANTIC_API_URL",
        "SEMANTIC_API_MODEL",
        "WEB_SEARCH_ENABLED",
        "WEB_SEARCH_API_URL",
        "WEB_SEARCH_API_KEY",
        "WEB_SEARCH_TIMEOUT_SECONDS",
        "MUTATION_INTENT_API_ENABLED",
        "VISION_API_ENABLED",
        "PROFILE_CALIBRATION_ENABLED",
        "DAILY_REVIEW_ENABLED",
        "FEISHU_CARD_ACTION_TRANSPORT",
        "FEISHU_CARD_CALLBACK_ENABLED",
        "CLAUDE_PARTIAL_MESSAGES_ENABLED",
    }
    assert required <= _example_keys()


def test_sync_env_adds_new_fields_without_overwriting_existing_secret(
    tmp_path, monkeypatch, capsys
):
    example = tmp_path / ".env.example"
    environment = tmp_path / ".env"
    backup = tmp_path / ".env.bak"
    example.write_text(
        "WEB_SEARCH_ENABLED=false\n"
        "WEB_SEARCH_API_URL=\n"
        "WEB_SEARCH_API_KEY=\n"
        "WEB_SEARCH_TIMEOUT_SECONDS=10\n",
        encoding="utf-8",
    )
    environment.write_text(
        "WEB_SEARCH_API_KEY=deployment-secret\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sync_env, "ENV_EXAMPLE", example)
    monkeypatch.setattr(sync_env, "ENV_FILE", environment)
    monkeypatch.setattr(sync_env, "ENV_BACKUP", backup)

    sync_env.main()

    merged = environment.read_text(encoding="utf-8")
    output = capsys.readouterr().out
    assert "WEB_SEARCH_API_KEY=deployment-secret" in merged
    assert "WEB_SEARCH_ENABLED=false" in merged
    assert "WEB_SEARCH_TIMEOUT_SECONDS=10" in merged
    assert "deployment-secret" not in output
    assert "WEB_SEARCH_API_KEY" in output
    assert ".env         = <configured>" in output
    assert backup.exists()
