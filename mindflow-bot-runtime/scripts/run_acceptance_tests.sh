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
BUILD_REVISION="$HOST_REVISION"
export BUILD_REVISION
export MINDFLOW_REQUIRE_POSTGRES_TESTS=1

RUNNING_BOT_REVISION=$(docker compose -f "$COMPOSE_FILE" exec -T bot \
  python3 -c "from app.build_info import BUILD_REVISION; print(BUILD_REVISION)")
RUNNING_ADMIN_REVISION=$(docker compose -f "$COMPOSE_FILE" exec -T admin \
  python3 -c "from app.build_info import BUILD_REVISION; print(BUILD_REVISION)")

if [ "$HOST_REVISION" != "$RUNNING_BOT_REVISION" ] || \
  [ "$HOST_REVISION" != "$RUNNING_ADMIN_REVISION" ]; then
  echo "revision mismatch before maintenance: host=$HOST_REVISION running_bot=$RUNNING_BOT_REVISION running_admin=$RUNNING_ADMIN_REVISION" >&2
  exit 4
fi

RUNTIME_STOPPED=0

restore_runtime() {
  if [ "$RUNTIME_STOPPED" = "1" ]; then
    echo "restoring bot and admin after acceptance maintenance window"
    if docker compose -f "$COMPOSE_FILE" up -d --no-deps bot admin; then
      RUNTIME_STOPPED=0
    else
      echo "failed to restore bot and admin" >&2
      return 1
    fi
  fi
}

trap restore_runtime EXIT HUP INT TERM

# Mark the runtime as needing restoration before stop so even a partial stop is
# recovered when docker compose returns an error.
RUNTIME_STOPPED=1
docker compose -f "$COMPOSE_FILE" stop bot admin

docker compose -f "$COMPOSE_FILE" build acceptance

ACCEPTANCE_IMAGE_REVISION=$(docker compose -f "$COMPOSE_FILE" run --rm --no-deps acceptance \
  python3 -c "from app.build_info import BUILD_REVISION; print(BUILD_REVISION)")

if [ "$ACCEPTANCE_IMAGE_REVISION" != "$HOST_REVISION" ]; then
  echo "acceptance image revision mismatch: host=$HOST_REVISION acceptance_image=$ACCEPTANCE_IMAGE_REVISION" >&2
  exit 4
fi

docker compose -f "$COMPOSE_FILE" run --rm --no-deps \
  -e MINDFLOW_TEST_POSTGRES_URL="$MINDFLOW_TEST_POSTGRES_URL" \
  -e DATABASE_URL="$MINDFLOW_TEST_POSTGRES_URL" \
  -e MINDFLOW_REQUIRE_POSTGRES_TESTS=1 \
  acceptance

restore_runtime

read_restored_revision() {
  service=$1
  attempts=0
  while [ "$attempts" -lt 12 ]; do
    if revision=$(docker compose -f "$COMPOSE_FILE" exec -T "$service" \
      python3 -c "from app.build_info import BUILD_REVISION; print(BUILD_REVISION)" 2>/dev/null); then
      printf '%s\n' "$revision"
      return 0
    fi
    attempts=$((attempts + 1))
    sleep 5
  done
  echo "$service did not become ready after acceptance" >&2
  return 1
}

RESTORED_BOT_REVISION=$(read_restored_revision bot)
RESTORED_ADMIN_REVISION=$(read_restored_revision admin)

if [ "$RESTORED_BOT_REVISION" != "$HOST_REVISION" ] || \
  [ "$RESTORED_ADMIN_REVISION" != "$HOST_REVISION" ]; then
  echo "revision mismatch after maintenance: host=$HOST_REVISION restored_bot=$RESTORED_BOT_REVISION restored_admin=$RESTORED_ADMIN_REVISION" >&2
  exit 4
fi
