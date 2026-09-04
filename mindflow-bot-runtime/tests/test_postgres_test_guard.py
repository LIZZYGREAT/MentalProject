import pytest

from app.postgres_test_guard import (
    configured_test_postgres_url,
    get_test_postgres_connect_timeout_seconds,
    main,
    optional_test_postgres_url,
    validate_configured_test_postgres_target,
    validate_test_postgres_url,
)


@pytest.mark.parametrize(
    "database_name",
    ["mindflow_acceptance_test", "mindflow_test_ci", "mindflow_test_20260828"],
)
def test_postgres_guard_accepts_only_documented_test_names(database_name):
    url = f"postgresql+psycopg://tester:secret@localhost/{database_name}"
    assert validate_test_postgres_url(url) == url


@pytest.mark.parametrize(
    "host",
    ["postgres", "localhost", "127.0.0.1", "[::1]"],
)
def test_postgres_guard_accepts_default_allowed_hosts(host):
    url = f"postgresql+psycopg://tester:secret@{host}/mindflow_test_ci"
    assert validate_test_postgres_url(url) == url


@pytest.mark.parametrize(
    "host",
    ["production-db.internal", "db.example.com", "10.0.0.99"],
)
def test_postgres_guard_rejects_unapproved_hosts_without_leaking_password(host):
    url = f"postgresql://tester:super-secret@{host}/mindflow_test_ci"
    with pytest.raises(ValueError, match="refusing PostgreSQL test host") as exc_info:
        validate_test_postgres_url(url)
    assert "super-secret" not in str(exc_info.value)


def test_postgres_guard_rejects_missing_host():
    with pytest.raises(ValueError, match="refusing PostgreSQL test host"):
        validate_test_postgres_url("postgresql:///mindflow_test_ci")


def test_postgres_guard_allows_explicit_additional_host(monkeypatch):
    monkeypatch.setenv(
        "MINDFLOW_TEST_POSTGRES_ALLOWED_HOSTS",
        "disposable-db.internal",
    )
    url = "postgresql://tester:secret@disposable-db.internal/mindflow_test_ci"
    assert validate_test_postgres_url(url) == url


@pytest.mark.parametrize(
    "database_name",
    ["mindflow", "contest", "mindflow_acceptance", "prod_test", "mindflow_test"],
)
def test_postgres_guard_rejects_ambiguous_or_production_names(database_name):
    with pytest.raises(ValueError, match="refusing PostgreSQL test"):
        validate_test_postgres_url(f"postgresql://tester:secret@localhost/{database_name}")


def test_postgres_guard_rejects_non_postgres_and_missing_values():
    with pytest.raises(ValueError, match="not configured"):
        validate_test_postgres_url("")
    with pytest.raises(ValueError, match="must use PostgreSQL"):
        validate_test_postgres_url("sqlite:///mindflow_test_ci")


def test_postgres_guard_never_falls_back_to_database_url(monkeypatch):
    monkeypatch.delenv("MINDFLOW_TEST_POSTGRES_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://owner:secret@production/mindflow_test_ci")
    with pytest.raises(ValueError, match="not configured"):
        configured_test_postgres_url()


def test_postgres_guard_can_skip_locally_but_fails_closed_for_acceptance(monkeypatch):
    monkeypatch.delenv("MINDFLOW_TEST_POSTGRES_URL", raising=False)
    monkeypatch.delenv("MINDFLOW_REQUIRE_POSTGRES_TESTS", raising=False)
    assert optional_test_postgres_url() is None

    monkeypatch.setenv("MINDFLOW_REQUIRE_POSTGRES_TESTS", "1")
    with pytest.raises(ValueError, match="required"):
        optional_test_postgres_url()


def test_postgres_connect_timeout_defaults_to_five_and_accepts_override(monkeypatch):
    monkeypatch.delenv("MINDFLOW_TEST_POSTGRES_CONNECT_TIMEOUT_SECONDS", raising=False)
    assert get_test_postgres_connect_timeout_seconds() == 5

    monkeypatch.setenv("MINDFLOW_TEST_POSTGRES_CONNECT_TIMEOUT_SECONDS", "12")
    assert get_test_postgres_connect_timeout_seconds() == 12


@pytest.mark.parametrize("value", ["", "zero", "0", "31", "-1", "1.5"])
def test_postgres_connect_timeout_rejects_invalid_values(monkeypatch, value):
    monkeypatch.setenv("MINDFLOW_TEST_POSTGRES_CONNECT_TIMEOUT_SECONDS", value)
    with pytest.raises(ValueError, match="between 1 and 30"):
        get_test_postgres_connect_timeout_seconds()


def test_full_target_validation_checks_host_and_timeout_before_success(monkeypatch):
    monkeypatch.setenv(
        "MINDFLOW_TEST_POSTGRES_URL",
        "postgresql://tester:secret@localhost/mindflow_test_ci",
    )
    monkeypatch.setenv("MINDFLOW_TEST_POSTGRES_CONNECT_TIMEOUT_SECONDS", "0")
    with pytest.raises(ValueError, match="between 1 and 30"):
        validate_configured_test_postgres_target()


def test_validator_cli_rejects_production_host_without_password_leak(
    monkeypatch,
    capsys,
):
    monkeypatch.setenv(
        "MINDFLOW_TEST_POSTGRES_URL",
        "postgresql://tester:super-secret@production-db.internal/mindflow_test_ci",
    )

    assert main() == 2
    captured = capsys.readouterr()
    assert "refusing PostgreSQL test host" in captured.err
    assert "super-secret" not in captured.err
