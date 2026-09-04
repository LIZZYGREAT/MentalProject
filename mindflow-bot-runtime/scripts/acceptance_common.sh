#!/usr/bin/env sh

# Shared fail-fast checks and revision helpers for Acceptance operations.

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
RUNTIME_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
PROJECT_ROOT=$(CDPATH= cd -- "$RUNTIME_ROOT/.." && pwd)
COMPOSE_FILE="$RUNTIME_ROOT/compose.yaml"
ACCEPTANCE_IMAGE_NAME=${ACCEPTANCE_IMAGE_NAME:-mindflow-acceptance:local}

export ACCEPTANCE_IMAGE_NAME

acceptance_error() {
  echo "$*" >&2
}

acceptance_warning() {
  echo "warning: $*" >&2
}

require_clean_working_tree() {
  if [ "${ALLOW_DIRTY_ACCEPTANCE:-0}" != "1" ] && \
    [ -n "$(git -C "$PROJECT_ROOT" status --porcelain)" ]; then
    acceptance_error "working tree is dirty; commit/push before acceptance"
    return 3
  fi
}

load_host_revision() {
  HOST_REVISION=$(git -C "$PROJECT_ROOT" rev-parse HEAD)
  BUILD_REVISION="$HOST_REVISION"
  export HOST_REVISION BUILD_REVISION
}

require_positive_integer() {
  setting_name=$1
  setting_value=$2
  case "$setting_value" in
    ''|*[!0-9]*|0)
      acceptance_error "$setting_name must be a positive integer"
      return 2
      ;;
  esac
}

check_recent_docker_fatals() {
  if ! command -v journalctl >/dev/null 2>&1; then
    acceptance_warning "journalctl is unavailable; skipping recent Docker fatal log check"
    return 0
  fi

  if ! DOCKER_JOURNAL=$(journalctl -u docker -b --since "10 minutes ago" --no-pager 2>&1); then
    acceptance_warning "cannot read current-boot Docker journal; relying on docker health checks"
    return 0
  fi

  if printf '%s\n' "$DOCKER_JOURNAL" | grep -Eiq \
    'permission denied|insufficient permissions|not permitted|no journal files were opened'; then
    acceptance_warning "cannot read current-boot Docker journal; relying on docker health checks"
    return 0
  fi

  if printf '%s\n' "$DOCKER_JOURNAL" | grep -Eiq \
    'only one connection allowed|healthcheck failed fatally|session healthcheck failed fatally'; then
    acceptance_error "Docker/BuildKit unhealthy; refusing acceptance operation"
    return 5
  fi
}

docker_health_preflight() {
  if ! docker info >/dev/null 2>&1; then
    acceptance_error "Docker/BuildKit unhealthy; refusing acceptance operation"
    return 5
  fi

  if ! BUILDER_STATUS=$(docker buildx inspect default 2>&1); then
    acceptance_error "Docker/BuildKit unhealthy; refusing acceptance operation"
    return 5
  fi
  if ! printf '%s\n' "$BUILDER_STATUS" | grep -Eq 'Status:[[:space:]]*running'; then
    acceptance_error "Docker/BuildKit unhealthy; refusing acceptance operation"
    return 5
  fi

  check_recent_docker_fatals
  echo "Docker preflight PASS"
}

disk_preflight() {
  ACCEPTANCE_MIN_FREE_DISK_MB=${ACCEPTANCE_MIN_FREE_DISK_MB:-10240}
  require_positive_integer ACCEPTANCE_MIN_FREE_DISK_MB "$ACCEPTANCE_MIN_FREE_DISK_MB"

  AVAILABLE_DISK_KB=$(df -Pk / | awk 'NR == 2 {print $4}')
  require_positive_integer root_available_disk_kb "$AVAILABLE_DISK_KB"
  REQUIRED_DISK_KB=$((ACCEPTANCE_MIN_FREE_DISK_MB * 1024))
  if [ "$AVAILABLE_DISK_KB" -lt "$REQUIRED_DISK_KB" ]; then
    acceptance_error "insufficient root disk space: available_kb=$AVAILABLE_DISK_KB required_kb=$REQUIRED_DISK_KB"
    return 5
  fi
  echo "Disk preflight PASS"
}

memory_preflight() {
  ACCEPTANCE_MIN_AVAILABLE_MEMORY_MB=${ACCEPTANCE_MIN_AVAILABLE_MEMORY_MB:-512}
  require_positive_integer ACCEPTANCE_MIN_AVAILABLE_MEMORY_MB "$ACCEPTANCE_MIN_AVAILABLE_MEMORY_MB"

  AVAILABLE_MEMORY_KB=$(awk '/^MemAvailable:/ {print $2; exit}' /proc/meminfo)
  require_positive_integer MemAvailable_kb "$AVAILABLE_MEMORY_KB"
  REQUIRED_MEMORY_KB=$((ACCEPTANCE_MIN_AVAILABLE_MEMORY_MB * 1024))
  if [ "$AVAILABLE_MEMORY_KB" -lt "$REQUIRED_MEMORY_KB" ]; then
    acceptance_error "insufficient available memory: available_kb=$AVAILABLE_MEMORY_KB required_kb=$REQUIRED_MEMORY_KB"
    return 5
  fi
  echo "Memory preflight PASS"
}

acceptance_preflight() {
  docker_health_preflight
  disk_preflight
  memory_preflight
}

read_running_revision() {
  service=$1
  docker compose -f "$COMPOSE_FILE" exec -T "$service" \
    python3 -c "from app.build_info import BUILD_REVISION; print(BUILD_REVISION)"
}

load_running_revisions() {
  RUNNING_BOT_REVISION=$(read_running_revision bot)
  RUNNING_ADMIN_REVISION=$(read_running_revision admin)
  export RUNNING_BOT_REVISION RUNNING_ADMIN_REVISION
}

require_running_revision_parity() {
  load_running_revisions
  if [ "$HOST_REVISION" != "$RUNNING_BOT_REVISION" ] || \
    [ "$HOST_REVISION" != "$RUNNING_ADMIN_REVISION" ]; then
    acceptance_error "revision mismatch before maintenance: host=$HOST_REVISION running_bot=$RUNNING_BOT_REVISION running_admin=$RUNNING_ADMIN_REVISION"
    return 4
  fi
}

load_acceptance_image_revision() {
  if ! ACCEPTANCE_IMAGE_ENV=$(docker image inspect \
    --format '{{range .Config.Env}}{{println .}}{{end}}' \
    "$ACCEPTANCE_IMAGE_NAME" 2>/dev/null); then
    return 1
  fi
  ACCEPTANCE_IMAGE_REVISION=$(printf '%s\n' "$ACCEPTANCE_IMAGE_ENV" | \
    sed -n 's/^BUILD_REVISION=//p' | head -n 1)
  [ -n "$ACCEPTANCE_IMAGE_REVISION" ] || return 1
  export ACCEPTANCE_IMAGE_REVISION
}

require_current_acceptance_image() {
  if ! load_acceptance_image_revision || \
    [ "$ACCEPTANCE_IMAGE_REVISION" != "$HOST_REVISION" ]; then
    acceptance_error "acceptance image missing or stale; run ./scripts/prepare_acceptance_image.sh first"
    return 4
  fi
}

validate_postgres_target_with_acceptance_image() {
  docker compose -f "$COMPOSE_FILE" run --rm --no-deps \
    -e MINDFLOW_TEST_POSTGRES_URL="${MINDFLOW_TEST_POSTGRES_URL:-}" \
    acceptance \
    python3 -m app.postgres_test_guard
}

cap_acceptance_build_cache() {
  ACCEPTANCE_BUILD_CACHE_KEEP=${ACCEPTANCE_BUILD_CACHE_KEEP:-6GB}
  if ! docker builder prune -a -f \
    --keep-storage "$ACCEPTANCE_BUILD_CACHE_KEEP"; then
    acceptance_warning "failed to cap Docker builder cache at $ACCEPTANCE_BUILD_CACHE_KEEP"
    return 1
  fi
}

restore_runtime() {
  if [ "${RUNTIME_STOPPED:-0}" = "1" ]; then
    echo "restoring bot and admin after acceptance maintenance window"
    if docker compose -f "$COMPOSE_FILE" up -d --no-deps bot admin; then
      RUNTIME_STOPPED=0
    else
      acceptance_error "failed to restore bot and admin"
      return 1
    fi
  fi
}

read_restored_revision() {
  service=$1
  attempts=0
  while [ "$attempts" -lt 12 ]; do
    if revision=$(read_running_revision "$service" 2>/dev/null); then
      printf '%s\n' "$revision"
      return 0
    fi
    attempts=$((attempts + 1))
    sleep 5
  done
  acceptance_error "$service did not become ready after acceptance"
  return 1
}

require_restored_revision_parity() {
  RESTORED_BOT_REVISION=$(read_restored_revision bot)
  RESTORED_ADMIN_REVISION=$(read_restored_revision admin)
  if [ "$RESTORED_BOT_REVISION" != "$HOST_REVISION" ] || \
    [ "$RESTORED_ADMIN_REVISION" != "$HOST_REVISION" ]; then
    acceptance_error "revision mismatch after maintenance: host=$HOST_REVISION restored_bot=$RESTORED_BOT_REVISION restored_admin=$RESTORED_ADMIN_REVISION"
    return 4
  fi
}
