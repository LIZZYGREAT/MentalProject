#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
. "$SCRIPT_DIR/acceptance_common.sh"

require_clean_working_tree
load_host_revision
acceptance_preflight
require_running_revision_parity

RUNTIME_STOPPED=0
BUILD_STARTED=0

prepare_cleanup() {
  original_status=$?
  trap - EXIT HUP INT TERM
  set +e

  if ! restore_runtime; then
    acceptance_warning "runtime restoration failed during prepare cleanup"
  fi

  if [ "${BUILD_STARTED:-0}" = "1" ]; then
    cap_acceptance_build_cache || true
  fi

  docker system df || acceptance_warning "failed to print docker system df"
  df -h / || acceptance_warning "failed to print root filesystem usage"
  docker ps || acceptance_warning "failed to print running containers"

  exit "$original_status"
}

trap prepare_cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

# Mark restoration as required before stop so a partial stop is recovered.
RUNTIME_STOPPED=1
docker compose -f "$COMPOSE_FILE" stop bot admin

BUILD_STARTED=1
docker compose -f "$COMPOSE_FILE" build acceptance

if ! load_acceptance_image_revision || \
  [ "$ACCEPTANCE_IMAGE_REVISION" != "$HOST_REVISION" ]; then
  acceptance_error "acceptance image revision mismatch: host=$HOST_REVISION acceptance_image=${ACCEPTANCE_IMAGE_REVISION:-missing}"
  exit 4
fi

restore_runtime
require_restored_revision_parity

echo "prepared acceptance image for revision $HOST_REVISION"
