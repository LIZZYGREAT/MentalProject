#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
. "$SCRIPT_DIR/acceptance_common.sh"

require_clean_working_tree
load_host_revision
acceptance_preflight
require_current_acceptance_image

BOT_CONTAINER_ID=$(docker compose -f "$COMPOSE_FILE" ps -q bot)
ADMIN_CONTAINER_ID=$(docker compose -f "$COMPOSE_FILE" ps -q admin)

require_running_container() {
  service=$1
  container_id=$2
  if [ -z "$container_id" ] || \
    [ "$(docker inspect --format '{{.State.Running}}' "$container_id" 2>/dev/null || true)" != "true" ]; then
    acceptance_error "$service must be running before and after Admin tests"
    return 5
  fi
}

require_running_container bot "$BOT_CONTAINER_ID"
require_running_container admin "$ADMIN_CONTAINER_ID"

set +e
docker compose -f "$COMPOSE_FILE" run --rm --no-deps \
  -e MINDFLOW_REQUIRE_POSTGRES_TESTS=0 \
  acceptance \
  python3 -m pytest -q \
  /srv/runtime/tests/test_admin_daily_review_dependency_refresh.py \
  /srv/runtime/tests/test_admin_forecast_visualization.py \
  /srv/runtime/tests/test_admin_security.py \
  /srv/runtime/tests/test_admin_visualization_contract.py \
  /srv/runtime/tests/test_admin_web.py
TEST_RESULT=$?
set -e

CURRENT_BOT_CONTAINER_ID=$(docker compose -f "$COMPOSE_FILE" ps -q bot)
CURRENT_ADMIN_CONTAINER_ID=$(docker compose -f "$COMPOSE_FILE" ps -q admin)
require_running_container bot "$CURRENT_BOT_CONTAINER_ID"
require_running_container admin "$CURRENT_ADMIN_CONTAINER_ID"

if [ "$BOT_CONTAINER_ID" != "$CURRENT_BOT_CONTAINER_ID" ] || \
  [ "$ADMIN_CONTAINER_ID" != "$CURRENT_ADMIN_CONTAINER_ID" ]; then
  acceptance_warning "bot or admin container changed independently while Admin tests ran"
fi

exit "$TEST_RESULT"
