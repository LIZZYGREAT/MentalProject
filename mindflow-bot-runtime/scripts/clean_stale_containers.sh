#!/usr/bin/env sh
set -eu

# Remove stopped one-shot containers and dangling images left behind by
# rebuilds (e.g. leftover migrate/claude-state-init runs, superseded image
# layers). By default it never touches:
#   - running containers (postgres, bot, admin)
#   - named volumes (postgres_data, claude_state)
#   - the Docker builder cache, which holds the mindflow-pip-cache BuildKit
#     cache mount; pruning it can force full requirement re-downloads.
# Use --build-cache only when reclaiming disk space outweighs the next-build
# cache hit. The cap remains configurable through ACCEPTANCE_BUILD_CACHE_KEEP.

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
RUNTIME_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)

usage() {
  cat <<EOF
usage: sh ./scripts/clean_stale_containers.sh [--dry-run] [--build-cache]

  --dry-run      only list what would be removed
  --build-cache  also cap the BuildKit cache; may require dependency downloads
                 during a later image build
EOF
}

DRY_RUN=0
PRUNE_BUILD_CACHE=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --build-cache) PRUNE_BUILD_CACHE=1 ;;
    -h | --help | help)
      usage
      exit 0
      ;;
    *)
      usage
      echo "unknown option: $1" >&2
      exit 2
      ;;
  esac
  shift
done

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

if [ "$PRUNE_BUILD_CACHE" = "1" ]; then
  ACCEPTANCE_BUILD_CACHE_KEEP=${ACCEPTANCE_BUILD_CACHE_KEEP:-6GB}
  echo
  echo "== BuildKit cache (keep $ACCEPTANCE_BUILD_CACHE_KEEP) =="
  if [ "$DRY_RUN" = "1" ]; then
    echo "dry run: BuildKit cache would be capped; this can evict the pip download cache"
  else
    docker builder prune -a -f --keep-storage "$ACCEPTANCE_BUILD_CACHE_KEEP"
  fi
fi

if [ "$DRY_RUN" = "1" ]; then
  echo
  echo "dry run finished; rerun without --dry-run to apply"
  exit 0
fi

echo
echo "== docker usage after cleanup =="
docker system df
