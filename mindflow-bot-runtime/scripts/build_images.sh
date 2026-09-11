#!/usr/bin/env sh
set -eu

# Build the migrate/bot/admin images for the current HEAD without touching
# the running services (docker compose build never recreates containers).
# Uses the same BUILD_REVISION injection as deploy_runtime.sh so a later
# deploy can reuse these images instead of rebuilding.

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
RUNTIME_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
PROJECT_ROOT=$(CDPATH= cd -- "$RUNTIME_ROOT/.." && pwd)
COMPOSE_FILE="$RUNTIME_ROOT/compose.yaml"

if [ -n "$(git -C "$PROJECT_ROOT" status --porcelain)" ]; then
  echo "working tree is dirty; commit/push before building" >&2
  exit 3
fi

BUILD_REVISION=$(git -C "$PROJECT_ROOT" rev-parse HEAD)
export BUILD_REVISION

echo "building migrate bot admin images for revision $BUILD_REVISION"
docker compose -f "$COMPOSE_FILE" build migrate bot admin

echo "images built; running services were not restarted"
echo "apply them with: sh ./scripts/deploy_runtime.sh"
