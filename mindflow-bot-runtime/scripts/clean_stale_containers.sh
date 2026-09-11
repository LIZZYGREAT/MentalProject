#!/usr/bin/env sh
set -eu

# Remove stopped one-shot containers and dangling images left behind by
# rebuilds (e.g. leftover migrate/claude-state-init runs, superseded image
# layers). Never touches:
#   - running containers (postgres, bot, admin)
#   - named volumes (postgres_data, claude_state)
#   - the Docker builder cache, which holds the mindflow-pip-cache BuildKit
#     cache mount; pruning it forces full requirement re-downloads.
#     Acceptance already caps it separately via ACCEPTANCE_BUILD_CACHE_KEEP.

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
RUNTIME_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)

usage() {
  cat <<EOF
usage: sh ./scripts/clean_stale_containers.sh [--dry-run]

  --dry-run  only list what would be removed
EOF
}

DRY_RUN=0
case ${1:-} in
  --dry-run) DRY_RUN=1 ;;
  -h | --help | help)
    usage
    exit 0
    ;;
  '') ;;
  *)
    usage
    echo "unknown option: $1" >&2
    exit 2
    ;;
esac

echo "== docker usage before cleanup =="
docker system df

echo
echo "== stopped containers (created/exited/dead) =="
docker ps -a --filter status=created --filter status=exited \
  --filter status=dead --format '{{.ID}}  {{.Names}}  {{.Status}}'
STOPPED_IDS=$(docker ps -aq --filter status=created --filter status=exited \
  --filter status=dead)
if [ "$DRY_RUN" = "1" ]; then
  echo "dry run: stopped containers listed above would be removed"
elif [ -n "$STOPPED_IDS" ]; then
  # shellcheck disable=SC2086
  docker container rm $STOPPED_IDS
else
  echo "no stopped containers to remove"
fi

echo
echo "== dangling (untagged <none>) images =="
docker images --filter dangling=true \
  --format '{{.ID}}  {{.Repository}}:{{.Tag}}  {{.CreatedSince}}  {{.Size}}'
if [ "$DRY_RUN" = "1" ]; then
  echo "dry run: dangling images listed above would be removed"
else
  docker image prune -f
fi

if [ "$DRY_RUN" = "1" ]; then
  echo
  echo "dry run finished; rerun without --dry-run to apply"
  exit 0
fi

echo
echo "== docker usage after cleanup =="
docker system df
