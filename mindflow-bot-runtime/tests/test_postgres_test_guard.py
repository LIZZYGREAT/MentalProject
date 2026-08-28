import pytest

from postgres_test_guard import (
    configured_test_postgres_url,
    optional_test_postgres_url,
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
