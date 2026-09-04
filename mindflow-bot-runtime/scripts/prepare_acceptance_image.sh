#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
. "$SCRIPT_DIR/acceptance_common.sh"

require_clean_working_tree
load_host_revision
acceptance_preflight
require_running_revision_parity

RUNTIME_STOPPED=0
trap restore_runtime EXIT HUP INT TERM

# Mark restoration as required before stop so a partial stop is recovered.
RUNTIME_STOPPED=1
docker compose -f "$COMPOSE_FILE" stop bot admin

docker compose -f "$COMPOSE_FILE" build acceptance

if ! load_acceptance_image_revision || \
  [ "$ACCEPTANCE_IMAGE_REVISION" != "$HOST_REVISION" ]; then
  acceptance_error "acceptance image revision mismatch: host=$HOST_REVISION acceptance_image=${ACCEPTANCE_IMAGE_REVISION:-missing}"
  exit 4
fi

restore_runtime
require_restored_revision_parity

ACCEPTANCE_BUILD_CACHE_KEEP=${ACCEPTANCE_BUILD_CACHE_KEEP:-6GB}
if ! docker builder prune -a -f --keep-storage "$ACCEPTANCE_BUILD_CACHE_KEEP"; then
  acceptance_warning "failed to cap Docker builder cache at $ACCEPTANCE_BUILD_CACHE_KEEP"
fi

docker system df
df -h /
docker ps

echo "prepared acceptance image for revision $HOST_REVISION"
