"""Fail-closed target validation for destructive PostgreSQL tests."""

from __future__ import annotations

import os
import sys

from sqlalchemy.engine import make_url


ALLOWED_EXACT_DATABASE = "mindflow_acceptance_test"
ALLOWED_DATABASE_PREFIX = "mindflow_test_"
DEFAULT_ALLOWED_TEST_POSTGRES_HOSTS = {
    "postgres",
    "localhost",
    "127.0.0.1",
    "::1",
}
DEFAULT_TEST_POSTGRES_CONNECT_TIMEOUT_SECONDS = 5
MIN_TEST_POSTGRES_CONNECT_TIMEOUT_SECONDS = 1
MAX_TEST_POSTGRES_CONNECT_TIMEOUT_SECONDS = 30


def allowed_test_postgres_hosts() -> set[str]:
    configured_hosts = {
        host.strip().lower()
        for host in os.environ.get("MINDFLOW_TEST_POSTGRES_ALLOWED_HOSTS", "").split(",")
        if host.strip()
    }
    return DEFAULT_ALLOWED_TEST_POSTGRES_HOSTS | configured_hosts


def get_test_postgres_connect_timeout_seconds() -> int:
    raw_timeout = os.environ.get(
        "MINDFLOW_TEST_POSTGRES_CONNECT_TIMEOUT_SECONDS",
        str(DEFAULT_TEST_POSTGRES_CONNECT_TIMEOUT_SECONDS),
    ).strip()
    try:
        timeout = int(raw_timeout)
    except ValueError as exc:
        raise ValueError(
            "MINDFLOW_TEST_POSTGRES_CONNECT_TIMEOUT_SECONDS must be an integer "
            "between 1 and 30"
        ) from exc
    if not (
        MIN_TEST_POSTGRES_CONNECT_TIMEOUT_SECONDS
        <= timeout
        <= MAX_TEST_POSTGRES_CONNECT_TIMEOUT_SECONDS
    ):
        raise ValueError(
            "MINDFLOW_TEST_POSTGRES_CONNECT_TIMEOUT_SECONDS must be between 1 and 30"
        )
    return timeout


def validate_test_postgres_url(raw_url: str) -> str:
    if not raw_url.strip():
        raise ValueError("MINDFLOW_TEST_POSTGRES_URL is not configured")
    try:
        parsed = make_url(raw_url)
    except Exception as exc:
        raise ValueError("MINDFLOW_TEST_POSTGRES_URL is invalid") from exc
    if not parsed.drivername.startswith("postgresql"):
        raise ValueError("MINDFLOW_TEST_POSTGRES_URL must use PostgreSQL")
    host = (parsed.host or "").strip().lower()
    if not host or host not in allowed_test_postgres_hosts():
        safe_host = host or "<missing>"
        raise ValueError(f"refusing PostgreSQL test host: {safe_host}")
    database_name = str(parsed.database or "")
    if (
        database_name != ALLOWED_EXACT_DATABASE
        and not database_name.startswith(ALLOWED_DATABASE_PREFIX)
    ):
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


def validate_configured_test_postgres_target() -> None:
    configured_test_postgres_url()
    get_test_postgres_connect_timeout_seconds()


def main() -> int:
    try:
        validate_configured_test_postgres_target()
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print("PostgreSQL test target validation PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
