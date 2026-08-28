#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
RUNTIME_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
PROJECT_ROOT=$(CDPATH= cd -- "$RUNTIME_ROOT/.." && pwd)
COMPOSE_FILE="$RUNTIME_ROOT/compose.yaml"

if [ -n "$(git -C "$PROJECT_ROOT" status --porcelain)" ]; then
  echo "working tree is dirty; commit/push before deployment" >&2
  exit 3
fi

BUILD_REVISION=$(git -C "$PROJECT_ROOT" rev-parse HEAD)
export BUILD_REVISION

docker compose -f "$COMPOSE_FILE" build migrate bot admin
docker compose -f "$COMPOSE_FILE" up -d postgres

POSTGRES_READY_ATTEMPT=0
until docker compose -f "$COMPOSE_FILE" exec -T postgres \
  pg_isready -U mindflow -d mindflow >/dev/null 2>&1; do
  POSTGRES_READY_ATTEMPT=$((POSTGRES_READY_ATTEMPT + 1))
  if [ "$POSTGRES_READY_ATTEMPT" -ge 30 ]; then
    echo "postgres did not become ready for deployment" >&2
    exit 5
  fi
  sleep 2
done

docker compose -f "$COMPOSE_FILE" run --rm --no-deps claude-state-init
docker compose -f "$COMPOSE_FILE" run --rm --no-deps migrate
docker compose -f "$COMPOSE_FILE" up -d --no-deps --force-recreate bot admin

RUNNING_BOT_REVISION=$(docker compose -f "$COMPOSE_FILE" exec -T bot \
  python3 -c "from app.build_info import BUILD_REVISION; print(BUILD_REVISION)")
RUNNING_ADMIN_REVISION=$(docker compose -f "$COMPOSE_FILE" exec -T admin \
  python3 -c "from app.build_info import BUILD_REVISION; print(BUILD_REVISION)")

if [ "$BUILD_REVISION" != "$RUNNING_BOT_REVISION" ] || \
  [ "$BUILD_REVISION" != "$RUNNING_ADMIN_REVISION" ]; then
  echo "deployment revision mismatch: expected=$BUILD_REVISION bot=$RUNNING_BOT_REVISION admin=$RUNNING_ADMIN_REVISION" >&2
  exit 4
fi

echo "deployed revision $BUILD_REVISION"
