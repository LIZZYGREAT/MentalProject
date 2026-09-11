#!/usr/bin/env sh
set -eu

# Fast-forward the server checkout to the latest pushed code.
# Pulling never touches running containers: they keep using the old image
# until ./scripts/deploy_runtime.sh rebuilds and recreates them.

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
RUNTIME_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
PROJECT_ROOT=$(CDPATH= cd -- "$RUNTIME_ROOT/.." && pwd)

if [ -n "$(git -C "$PROJECT_ROOT" status --porcelain)" ]; then
  echo "working tree is dirty; commit/push or stash before pulling" >&2
  exit 3
fi

BRANCH=$(git -C "$PROJECT_ROOT" rev-parse --abbrev-ref HEAD)
OLD_REVISION=$(git -C "$PROJECT_ROOT" rev-parse HEAD)
echo "branch=$BRANCH revision=$(git -C "$PROJECT_ROOT" rev-parse --short "$OLD_REVISION")"

echo "fetching origin"
git -C "$PROJECT_ROOT" fetch origin

echo "pulling (fast-forward only)"
git -C "$PROJECT_ROOT" pull --ff-only

NEW_REVISION=$(git -C "$PROJECT_ROOT" rev-parse HEAD)
if [ "$OLD_REVISION" = "$NEW_REVISION" ]; then
  echo "already up to date at $NEW_REVISION"
  exit 0
fi

echo
echo "new commits:"
git -C "$PROJECT_ROOT" log --oneline "$OLD_REVISION..$NEW_REVISION"

echo
echo "next steps:"
echo "  sh ./scripts/build_images.sh   # rebuild images without restarting services"
echo "  sh ./scripts/deploy_runtime.sh # apply: migrate, recreate bot/admin, verify revision"
