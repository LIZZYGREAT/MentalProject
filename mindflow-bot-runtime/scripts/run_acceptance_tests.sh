#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
RUNTIME_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
PROJECT_ROOT=$(CDPATH= cd -- "$RUNTIME_ROOT/.." && pwd)
COMPOSE_FILE="$RUNTIME_ROOT/compose.yaml"

if [ -z "${MINDFLOW_TEST_POSTGRES_URL:-}" ]; then
  echo "MINDFLOW_TEST_POSTGRES_URL is required for acceptance" >&2
  exit 2
fi

HOST_REVISION=$(git -C "$PROJECT_ROOT" rev-parse HEAD)
export BUILD_REVISION="$HOST_REVISION"
export MINDFLOW_REQUIRE_POSTGRES_TESTS=1

docker compose -f "$COMPOSE_FILE" build bot admin >/dev/null

BOT_REVISION=$(docker compose -f "$COMPOSE_FILE" run --rm --no-deps bot \
  python -c "from app.build_info import BUILD_REVISION; print(BUILD_REVISION)")
ADMIN_REVISION=$(docker compose -f "$COMPOSE_FILE" run --rm --no-deps admin \
  python -c "from app.build_info import BUILD_REVISION; print(BUILD_REVISION)")

if [ "$HOST_REVISION" != "$BOT_REVISION" ] || [ "$HOST_REVISION" != "$ADMIN_REVISION" ]; then
  echo "revision mismatch: host=$HOST_REVISION bot=$BOT_REVISION admin=$ADMIN_REVISION" >&2
  exit 3
fi

docker compose -f "$COMPOSE_FILE" run --rm --no-deps \
  -e MINDFLOW_TEST_POSTGRES_URL="$MINDFLOW_TEST_POSTGRES_URL" \
  -e MINDFLOW_REQUIRE_POSTGRES_TESTS=1 \
  -v "$RUNTIME_ROOT/tests:/srv/runtime/tests:ro" \
  -v "$PROJECT_ROOT/claude-runtime:/srv/claude-runtime:ro" \
  -v "$PROJECT_ROOT/docs:/srv/project/docs:ro" \
  bot python -m pytest -q \
    tests/test_synthetic_data_operations.py \
    tests/test_postgres_test_guard.py \
    tests/test_postgres_concurrency.py \
    tests/test_postgres_migration_0016_0017.py \
    tests/test_postgres_synthetic_cleanup.py \
    tests/test_admin_forecast_visualization.py \
    tests/test_progress_ordering.py \
    tests/test_response_presentation.py \
    tests/test_date_forecast.py
