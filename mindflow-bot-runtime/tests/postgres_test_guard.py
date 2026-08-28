"""Fail-closed PostgreSQL target validation shared by destructive DB tests."""

from __future__ import annotations

import os

from sqlalchemy.engine import make_url


ALLOWED_EXACT_DATABASE = "mindflow_acceptance_test"
ALLOWED_DATABASE_PREFIX = "mindflow_test_"


def validate_test_postgres_url(raw_url: str) -> str:
    if not raw_url.strip():
        raise ValueError("MINDFLOW_TEST_POSTGRES_URL is not configured")
    parsed = make_url(raw_url)
    if not parsed.drivername.startswith("postgresql"):
        raise ValueError("MINDFLOW_TEST_POSTGRES_URL must use PostgreSQL")
    database_name = str(parsed.database or "")
    if database_name != ALLOWED_EXACT_DATABASE and not database_name.startswith(ALLOWED_DATABASE_PREFIX):
        raise ValueError(
            "refusing PostgreSQL test outside mindflow_acceptance_test or mindflow_test_*"
        )
    return raw_url


def configured_test_postgres_url() -> str:
    """Read only the dedicated variable; DATABASE_URL is intentionally ignored."""

    return validate_test_postgres_url(os.environ.get("MINDFLOW_TEST_POSTGRES_URL", ""))


def optional_test_postgres_url() -> str | None:
    """Return None for local opt-out, but fail acceptance when PostgreSQL is required."""

    raw_url = os.environ.get("MINDFLOW_TEST_POSTGRES_URL", "").strip()
    if not raw_url:
        if os.environ.get("MINDFLOW_REQUIRE_POSTGRES_TESTS", "").strip() == "1":
            raise ValueError(
                "MINDFLOW_TEST_POSTGRES_URL is required because "
                "MINDFLOW_REQUIRE_POSTGRES_TESTS=1"
            )
        return None
    return validate_test_postgres_url(raw_url)
