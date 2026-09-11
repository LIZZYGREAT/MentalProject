#!/usr/bin/env sh
set -eu

# One-page server health check: host resources, git checkout state, Compose
# services, running image revisions vs HEAD, Alembic migration state, and
# Docker disk usage. Sections degrade individually instead of aborting.

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
RUNTIME_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
PROJECT_ROOT=$(CDPATH= cd -- "$RUNTIME_ROOT/.." && pwd)
COMPOSE_FILE="$RUNTIME_ROOT/compose.yaml"

case ${1:-} in
  -h | --help | help)
    echo "usage: sh ./scripts/status_report.sh"
    exit 0
    ;;
  '') ;;
  *)
    echo "unknown option: $1" >&2
    echo "usage: sh ./scripts/status_report.sh" >&2
    exit 2
    ;;
esac

section() {
  echo
  echo "== $* =="
}

warn() {
  echo "warning: $*" >&2
}

run_or_warn() {
  description=$1
  shift
  if "$@"; then
    return 0
  fi
  warn "$description failed"
}

section "host"
date
uptime || warn "uptime unavailable"
df -h / || warn "df unavailable"
AVAILABLE_MEMORY_MB=$(awk '/^MemAvailable:/ {printf "%.0f", $2 / 1024}' /proc/meminfo 2>/dev/null || echo '?')
echo "mem_available_mb=$AVAILABLE_MEMORY_MB"

section "git checkout"
BRANCH=$(git -C "$PROJECT_ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null \
  || echo unknown)
HOST_REVISION=$(git -C "$PROJECT_ROOT" rev-parse HEAD 2>/dev/null \
  || echo unknown)
echo "branch=$BRANCH"
echo "head=$HOST_REVISION"
if [ -n "$(git -C "$PROJECT_ROOT" status --porcelain)" ]; then
  warn "working tree is dirty; deploy_runtime.sh will refuse to deploy"
fi

section "compose services"
run_or_warn "docker compose ps" docker compose -f "$COMPOSE_FILE" ps

section "running service revisions vs head"
for service in bot admin; do
  REVISION=$(docker compose -f "$COMPOSE_FILE" exec -T "$service" \
    python3 -c "from app.build_info import BUILD_REVISION; print(BUILD_REVISION)" \
    2>/dev/null || echo unavailable)
  echo "$service=$REVISION"
  if [ "$REVISION" != "$HOST_REVISION" ]; then
    warn "$service is not running head revision ($HOST_REVISION); deploy if intended"
  fi
done

section "alembic migration state (inside bot)"
run_or_warn "alembic current" docker compose -f "$COMPOSE_FILE" exec -T bot \
  alembic current
ALEMBIC_HEADS=$(docker compose -f "$COMPOSE_FILE" exec -T bot \
  alembic heads 2>/dev/null | awk '{print $1}' | head -n 1)
ALEMBIC_CURRENT=$(docker compose -f "$COMPOSE_FILE" exec -T bot \
  alembic current 2>/dev/null | awk '{print $1}' | head -n 1)
if [ -n "$ALEMBIC_HEADS" ] && [ -n "$ALEMBIC_CURRENT" ]; then
  if [ "$ALEMBIC_CURRENT" = "$ALEMBIC_HEADS" ]; then
    echo "database is at the latest migration ($ALEMBIC_CURRENT)"
  else
    warn "database migration current=$ALEMBIC_CURRENT head=$ALEMBIC_HEADS; run deploy_runtime.sh"
  fi
else
  warn "could not determine alembic state (bot container not reachable?)"
fi

section "postgres health"
run_or_warn "pg_isready" docker compose -f "$COMPOSE_FILE" exec -T postgres \
  pg_isready -U mindflow -d mindflow

section "docker usage"
run_or_warn "docker system df" docker system df

echo
echo "status report finished"
