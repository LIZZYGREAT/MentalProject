#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
. "$SCRIPT_DIR/acceptance_common.sh"

require_clean_working_tree

if [ -z "${MINDFLOW_TEST_POSTGRES_URL:-}" ]; then
  acceptance_error "MINDFLOW_TEST_POSTGRES_URL is required for acceptance"
  exit 2
fi

case "$MINDFLOW_TEST_POSTGRES_URL" in
  postgresql://*|postgresql+*://*) ;;
  *)
    acceptance_error "MINDFLOW_TEST_POSTGRES_URL must use PostgreSQL"
    exit 2
    ;;
esac
TEST_POSTGRES_WITHOUT_QUERY=$(printf '%s\n' "$MINDFLOW_TEST_POSTGRES_URL" | sed 's/[?].*$//')
TEST_POSTGRES_DATABASE=${TEST_POSTGRES_WITHOUT_QUERY##*/}
case "$TEST_POSTGRES_DATABASE" in
  mindflow_acceptance_test|mindflow_test_*) ;;
  *)
    acceptance_error "refusing acceptance tests outside mindflow_acceptance_test or mindflow_test_*"
    exit 2
    ;;
esac

load_host_revision
export MINDFLOW_REQUIRE_POSTGRES_TESTS=1
acceptance_preflight
require_running_revision_parity
require_current_acceptance_image

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
