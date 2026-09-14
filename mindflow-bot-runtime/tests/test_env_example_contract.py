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


def _example_values() -> dict[str, str]:
    values = {}
    for line in (RUNTIME_ROOT / ".env.example").read_text(
        encoding="utf-8-sig"
    ).splitlines():
        parsed = sync_env.parse_env_assignment(line)
        if parsed:
            values[parsed["key"]] = sync_env.normalize_value(parsed["value"])
    return values


def test_env_example_contains_critical_runtime_capabilities():
    required = {
        "SEMANTIC_API_ENABLED",
        "SEMANTIC_API_URL",
        "SEMANTIC_API_MODEL",
        "WEB_SEARCH_ENABLED",
        "WEB_SEARCH_PROVIDER",
        "WEB_SEARCH_MODEL",
        "WEB_SEARCH_TIMEOUT_SECONDS",
        "WEB_SEARCH_MAX_USES",
        "WEB_SEARCH_MAX_OUTPUT_TOKENS",
        "WEB_SEARCH_RETRY_MAX_OUTPUT_TOKENS",
        "WEB_SEARCH_SUMMARY_MAX_CHARS",
        "MUTATION_INTENT_API_ENABLED",
        "VISION_API_ENABLED",
        "PROFILE_CALIBRATION_ENABLED",
        "DAILY_REVIEW_ENABLED",
        "FEISHU_CARD_ACTION_TRANSPORT",
        "FEISHU_CARD_CALLBACK_ENABLED",
        "CARD_ACTION_RECEIPT_TTL_HOURS",
        "CLAUDE_PARTIAL_MESSAGES_ENABLED",
    }
    assert required <= _example_keys()


def test_card_action_receipt_ttl_example_is_bounded():
    value = int(_example_values()["CARD_ACTION_RECEIPT_TTL_HOURS"])

    assert 24 <= value <= 720


def test_enabled_mutation_intent_has_nonempty_model_in_env_example():
    values = _example_values()

    assert values["MUTATION_INTENT_API_ENABLED"] == "true"
    assert values["MUTATION_INTENT_API_MODEL"] == "deepseek-v4-flash"


def test_env_example_feature_flags_are_internally_consistent():
    values = _example_values()

    def enabled(key: str) -> bool:
        return values.get(key, "").lower() == "true"

    if enabled("SEMANTIC_API_ENABLED"):
        assert values["SEMANTIC_API_URL"]
        assert values["SEMANTIC_API_MODEL"]
    if enabled("WEB_SEARCH_ENABLED"):
        assert values["WEB_SEARCH_PROVIDER"]
        assert values["WEB_SEARCH_MODEL"]
        assert "WEB_SEARCH_API_KEY" not in values
    if enabled("MUTATION_INTENT_API_ENABLED"):
        assert values["MUTATION_INTENT_API_URL"]
        assert values["MUTATION_INTENT_API_MODEL"]
    if enabled("VISION_API_ENABLED"):
        assert values["VISION_API_URL"]
        assert values["VISION_API_MODEL"]
    if enabled("DAILY_REVIEW_ENABLED"):
        assert values["DAILY_REVIEW_LOCAL_TIME"]
        assert values["DAILY_REVIEW_TIMEZONE"]
        assert (
            values["FEISHU_CARD_ACTION_TRANSPORT"] == "ws"
            or enabled("FEISHU_CARD_CALLBACK_ENABLED")
        )


def test_sync_env_adds_new_fields_without_overwriting_existing_secret(
    tmp_path, monkeypatch, capsys
):
    example = tmp_path / ".env.example"
    environment = tmp_path / ".env"
    backup = tmp_path / ".env.bak"
    example.write_text(
        "WEB_SEARCH_ENABLED=false\n"
        "WEB_SEARCH_PROVIDER=deepseek_native\n"
        "WEB_SEARCH_MODEL=deepseek-v4-flash\n"
        "WEB_SEARCH_TIMEOUT_SECONDS=20\n"
        "WEB_SEARCH_MAX_USES=3\n"
        "WEB_SEARCH_MAX_OUTPUT_TOKENS=1200\n",
        encoding="utf-8",
    )
    environment.write_text(
        "DEEPSEEK_API_KEY=deployment-secret\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sync_env, "ENV_EXAMPLE", example)
    monkeypatch.setattr(sync_env, "ENV_FILE", environment)
    monkeypatch.setattr(sync_env, "ENV_BACKUP", backup)

    sync_env.main()

    merged = environment.read_text(encoding="utf-8")
    output = capsys.readouterr().out
    assert "DEEPSEEK_API_KEY=deployment-secret" in merged
    assert "WEB_SEARCH_ENABLED=false" in merged
    assert "WEB_SEARCH_PROVIDER=deepseek_native" in merged
    assert "WEB_SEARCH_TIMEOUT_SECONDS=20" in merged
    assert "deployment-secret" not in output
    assert "DEEPSEEK_API_KEY" in output
    assert "! DEEPSEEK_API_KEY=<configured>" in output
    assert backup.exists()
