#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
. "$SCRIPT_DIR/acceptance_common.sh"

require_clean_working_tree
load_host_revision
export MINDFLOW_REQUIRE_POSTGRES_TESTS=1
acceptance_preflight
require_running_revision_parity
require_current_acceptance_image
validate_postgres_target_with_acceptance_image

RUNTIME_STOPPED=0

trap restore_runtime EXIT HUP INT TERM

# Mark the runtime as needing restoration before stop so even a partial stop is
# recovered when docker compose returns an error.
RUNTIME_STOPPED=1
docker compose -f "$COMPOSE_FILE" stop bot admin

set +e
docker compose -f "$COMPOSE_FILE" run --rm --no-deps \
  -e MINDFLOW_TEST_POSTGRES_URL="$MINDFLOW_TEST_POSTGRES_URL" \
  -e DATABASE_URL="$MINDFLOW_TEST_POSTGRES_URL" \
  -e MINDFLOW_REQUIRE_POSTGRES_TESTS=1 \
  acceptance
TEST_RESULT=$?
set -e

restore_runtime
require_restored_revision_parity

exit "$TEST_RESULT"
