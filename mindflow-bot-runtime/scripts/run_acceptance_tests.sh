#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
RUNTIME_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
PROJECT_ROOT=$(CDPATH= cd -- "$RUNTIME_ROOT/.." && pwd)
COMPOSE_FILE="$RUNTIME_ROOT/compose.yaml"

if [ "${ALLOW_DIRTY_ACCEPTANCE:-0}" != "1" ] && \
  [ -n "$(git -C "$PROJECT_ROOT" status --porcelain)" ]; then
  echo "working tree is dirty; commit/push before acceptance" >&2
  exit 3
fi

if [ -z "${MINDFLOW_TEST_POSTGRES_URL:-}" ]; then
  echo "MINDFLOW_TEST_POSTGRES_URL is required for acceptance" >&2
  exit 2
fi

case "$MINDFLOW_TEST_POSTGRES_URL" in
  postgresql://*|postgresql+*://*) ;;
  *)
    echo "MINDFLOW_TEST_POSTGRES_URL must use PostgreSQL" >&2
    exit 2
    ;;
esac
TEST_POSTGRES_WITHOUT_QUERY=$(printf '%s\n' "$MINDFLOW_TEST_POSTGRES_URL" | sed 's/[?].*$//')
TEST_POSTGRES_DATABASE=${TEST_POSTGRES_WITHOUT_QUERY##*/}
case "$TEST_POSTGRES_DATABASE" in
  mindflow_acceptance_test|mindflow_test_*) ;;
  *)
    echo "refusing acceptance tests outside mindflow_acceptance_test or mindflow_test_*" >&2
    exit 2
    ;;
esac

HOST_REVISION=$(git -C "$PROJECT_ROOT" rev-parse HEAD)
export MINDFLOW_REQUIRE_POSTGRES_TESTS=1

RUNNING_BOT_REVISION=$(docker compose -f "$COMPOSE_FILE" exec -T bot \
  python3 -c "from app.build_info import BUILD_REVISION; print(BUILD_REVISION)")
RUNNING_ADMIN_REVISION=$(docker compose -f "$COMPOSE_FILE" exec -T admin \
  python3 -c "from app.build_info import BUILD_REVISION; print(BUILD_REVISION)")
ACCEPTANCE_IMAGE_REVISION=$(docker compose -f "$COMPOSE_FILE" run --rm --no-deps acceptance \
  python3 -c "from app.build_info import BUILD_REVISION; print(BUILD_REVISION)")

if [ "$HOST_REVISION" != "$RUNNING_BOT_REVISION" ] || \
  [ "$HOST_REVISION" != "$RUNNING_ADMIN_REVISION" ] || \
  [ "$HOST_REVISION" != "$ACCEPTANCE_IMAGE_REVISION" ]; then
  echo "revision mismatch: host=$HOST_REVISION running_bot=$RUNNING_BOT_REVISION running_admin=$RUNNING_ADMIN_REVISION acceptance_image=$ACCEPTANCE_IMAGE_REVISION" >&2
  exit 4
fi

docker compose -f "$COMPOSE_FILE" run --rm --no-deps \
  --user root \
  -e MINDFLOW_TEST_POSTGRES_URL="$MINDFLOW_TEST_POSTGRES_URL" \
  -e DATABASE_URL="$MINDFLOW_TEST_POSTGRES_URL" \
  -e MINDFLOW_REQUIRE_POSTGRES_TESTS=1 \
  -v "$RUNTIME_ROOT/tests:/srv/runtime/tests:ro" \
  -v "$PROJECT_ROOT/claude-runtime:/srv/claude-runtime:ro" \
  -v "$PROJECT_ROOT/docs:/srv/docs:ro" \
  acceptance sh -eu -c '
    test -f /srv/docs/CURRENT_ARCHITECTURE.md
    test -f /srv/claude-runtime/plugins/mindflow-care/skills/mental-health-care/SKILL.md
    python3 -m pip install \
      --quiet \
      --no-cache-dir \
      --index-url https://pypi.tuna.tsinghua.edu.cn/simple \
      --trusted-host pypi.tuna.tsinghua.edu.cn \
      pytest==8.4.1 \
      pytest-asyncio==1.1.0
    exec python3 -m pytest -q tests
  '
