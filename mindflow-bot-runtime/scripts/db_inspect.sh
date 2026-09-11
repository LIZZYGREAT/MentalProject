#!/usr/bin/env sh
set -eu

# Read-only database inspection for the production Compose stack.
# All queries run through psql inside the running postgres service and never
# mutate data. The postgres connection literals match compose.yaml
# (POSTGRES_DB=mindflow, POSTGRES_USER=mindflow, healthcheck pg_isready).

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
RUNTIME_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
COMPOSE_FILE="$RUNTIME_ROOT/compose.yaml"

DEFAULT_PARTICIPANT_CODE=P002

usage() {
  cat <<'EOF'
usage: sh ./scripts/db_inspect.sh <command> [args]

read-only commands (participant code defaults to P002):
  summary [CODE]            participant row, binding, token, consent, row counts
  tables                   all tables with live row estimates + alembic version
  messages [CODE] [N]      latest conversation_messages (default N=20)
  events [CODE|all] [N]    latest bot_events, default all participants (N=20)
  observations [CODE] [N]  latest state_observations (default N=20)
  forecast [CODE] [N]      latest forecast_snapshots (default N=20)
  slow-state [CODE] [N]    latest participant_slow_states (default N=20)
  sessions [CODE]          claude_sessions for one participant
  schedule-images [CODE] [N]  latest course_schedule_image_sessions (N=20)
  schedule-imports [CODE] [N] latest course_schedule_imports (N=20)
  sql 'SELECT ...'         one read-only SELECT/WITH statement, nothing else
EOF
}

die() {
  echo "$*" >&2
  exit "${2:-2}"
}

psql_exec() {
  docker compose -f "$COMPOSE_FILE" exec -T postgres \
    psql -U mindflow -d mindflow -X -v ON_ERROR_STOP=1 "$@"
}

section() {
  echo
  echo "== $* =="
}

valid_participant_code() {
  case "$1" in
    '' | *[!A-Za-z0-9_-]*)
      echo "invalid participant code: $1" >&2
      return 2
      ;;
  esac
}

valid_positive_integer() {
  case "$1" in
    '' | *[!0-9]* | 0)
      echo "invalid positive integer: $1" >&2
      return 2
      ;;
  esac
}

participant_id_or_die() {
  code=$1
  valid_participant_code "$code" || return 2
  id=$(psql_exec -t -A -c \
    "SELECT id FROM participants WHERE participant_code = '$code';")
  if [ -z "$id" ]; then
    echo "participant not found: $code" >&2
    return 4
  fi
  printf '%s\n' "$id"
}

# SELECT/WITH-only guard for the free-form sql command: rejects stacked
# statements and data-modifying keywords even inside a CTE body.
assert_read_only_query() {
  query=$(printf '%s' "$1" | sed 's/;[[:space:]]*$//' | tr -d '\n')
  stripped=$(printf '%s' "$query" | sed 's/^[[:space:]]*//')
  case "$stripped" in
    select* | SELECT* | with* | WITH*) ;;
    *)
      echo "only a single SELECT/WITH statement is allowed" >&2
      return 2
      ;;
  esac
  case "$query" in
    *';'*)
      echo "statement must not contain internal semicolons" >&2
      return 2
      ;;
  esac
  if printf '%s' "$query" | grep -Eiq \
    '(^|[^a-zA-Z_])(insert|update|delete|merge|drop|alter|truncate|create|grant|revoke|copy|vacuum|analyze|call|do|reindex|checkpoint|cluster|listen|notify|lock|set|reset|begin|commit|rollback|savepoint|prepare|execute|deallocate|discard|into)([^a-zA-Z_]|$)'; then
    echo "query contains a write or DDL keyword; refusing" >&2
    return 2
  fi
}

command=${1:-}
[ -n "$command" ] || {
  usage
  exit 2
}
shift

case "$command" in
  summary)
    code=${1:-$DEFAULT_PARTICIPANT_CODE}
    pid=$(participant_id_or_die "$code") || exit $?
    section "participant $code"
    psql_exec -c \
      "SELECT participant_code, status, external_llm_consent_at, created_at FROM participants WHERE id = '$pid';"
    section "feishu binding"
    psql_exec -c \
      "SELECT app_id, open_id, chat_id, bound_at FROM feishu_bindings WHERE participant_id = '$pid';"
    section "calendar oauth token"
    psql_exec -c \
      "SELECT access_token_expires_at, refresh_token_expires_at, token_version, updated_at FROM feishu_oauth_tokens WHERE participant_id = '$pid';"
    section "row counts"
    psql_exec -c \
      "SELECT (SELECT count(*) FROM conversation_messages WHERE participant_id = '$pid') AS messages, (SELECT count(*) FROM state_observations WHERE participant_id = '$pid') AS observations, (SELECT count(*) FROM forecast_snapshots WHERE participant_id = '$pid') AS forecasts, (SELECT count(*) FROM bot_events WHERE participant_id = '$pid') AS bot_events, (SELECT count(*) FROM course_schedule_imports WHERE participant_id = '$pid') AS schedule_imports;"
    section "latest claude sessions"
    psql_exec -c \
      "SELECT session_id, status, last_message_id, created_at, updated_at FROM claude_sessions WHERE participant_id = '$pid' ORDER BY updated_at DESC LIMIT 3;"
    ;;

  tables)
    section "table row estimates"
    psql_exec -c \
      "SELECT relname AS table_name, n_live_tup AS row_estimate FROM pg_stat_user_tables ORDER BY relname;"
    section "alembic version"
    psql_exec -c "SELECT version_num FROM alembic_version;"
    ;;

  messages)
    code=${1:-$DEFAULT_PARTICIPANT_CODE}
    limit=${2:-20}
    valid_positive_integer "$limit" || exit 2
    pid=$(participant_id_or_die "$code") || exit $?
    psql_exec -c \
      "SELECT created_at, role, left(content, 120) AS content FROM conversation_messages WHERE participant_id = '$pid' ORDER BY created_at DESC LIMIT $limit;"
    ;;

  events)
    code=${1:-all}
    limit=${2:-20}
    valid_positive_integer "$limit" || exit 2
    if [ "$code" = "all" ]; then
      psql_exec -c \
        "SELECT e.received_at, e.status, e.message_type, COALESCE(p.participant_code, '(unbound)') AS participant, e.error_code, left(e.text, 60) AS text FROM bot_events e LEFT JOIN participants p ON p.id = e.participant_id ORDER BY e.received_at DESC LIMIT $limit;"
    else
      pid=$(participant_id_or_die "$code") || exit $?
      psql_exec -c \
        "SELECT e.received_at, e.status, e.message_type, e.error_code, left(e.text, 60) AS text, e.processed_at FROM bot_events e WHERE e.participant_id = '$pid' ORDER BY e.received_at DESC LIMIT $limit;"
    fi
    ;;

  observations)
    code=${1:-$DEFAULT_PARTICIPANT_CODE}
    limit=${2:-20}
    valid_positive_integer "$limit" || exit 2
    pid=$(participant_id_or_die "$code") || exit $?
    psql_exec -c \
      "SELECT observed_at, observation_type, left(payload_json::text, 100) AS payload FROM state_observations WHERE participant_id = '$pid' ORDER BY observed_at DESC LIMIT $limit;"
    ;;

  forecast)
    code=${1:-$DEFAULT_PARTICIPANT_CODE}
    limit=${2:-20}
    valid_positive_integer "$limit" || exit 2
    pid=$(participant_id_or_die "$code") || exit $?
    psql_exec -c \
      "SELECT local_date, algorithm_version, forecast_version, semantic_status, valid, generated_at FROM forecast_snapshots WHERE participant_id = '$pid' ORDER BY generated_at DESC LIMIT $limit;"
    ;;

  slow-state)
    code=${1:-$DEFAULT_PARTICIPANT_CODE}
    limit=${2:-20}
    valid_positive_integer "$limit" || exit 2
    pid=$(participant_id_or_die "$code") || exit $?
    psql_exec -c \
      "SELECT effective_at, cadence, rolling_7d_stress, rolling_7d_workload, rolling_7d_energy, recent_recovery_quality, recent_sleep_debt, source FROM participant_slow_states WHERE participant_id = '$pid' ORDER BY effective_at DESC LIMIT $limit;"
    ;;

  sessions)
    code=${1:-$DEFAULT_PARTICIPANT_CODE}
    pid=$(participant_id_or_die "$code") || exit $?
    psql_exec -c \
      "SELECT session_id, status, last_message_id, created_at, updated_at FROM claude_sessions WHERE participant_id = '$pid' ORDER BY updated_at DESC;"
    ;;

  schedule-images)
    code=${1:-$DEFAULT_PARTICIPANT_CODE}
    limit=${2:-20}
    valid_positive_integer "$limit" || exit 2
    pid=$(participant_id_or_die "$code") || exit $?
    psql_exec -c \
      "SELECT created_at, status, last_error_code, left(error_detail, 80) AS error_detail, vision_model, updated_at, expires_at FROM course_schedule_image_sessions WHERE participant_id = '$pid' ORDER BY updated_at DESC LIMIT $limit;"
    ;;

  schedule-imports)
    code=${1:-$DEFAULT_PARTICIPANT_CODE}
    limit=${2:-20}
    valid_positive_integer "$limit" || exit 2
    pid=$(participant_id_or_die "$code") || exit $?
    psql_exec -c \
      "SELECT created_at, status, recurrence_strategy, vision_model, confirmed_at, completed_at, cancel_mode, cleanup_error_code FROM course_schedule_imports WHERE participant_id = '$pid' ORDER BY created_at DESC LIMIT $limit;"
    ;;

  sql)
    [ $# -ge 1 ] || die "sql command needs a query argument"
    assert_read_only_query "$1" || exit 2
    psql_exec -c "$1"
    ;;

  -h | --help | help)
    usage
    exit 0
    ;;

  *)
    usage
    die "unknown command: $command"
    ;;
esac
