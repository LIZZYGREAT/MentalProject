from pathlib import Path


RUNNER = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "run_acceptance_tests.sh"
)
DEPLOY = RUNNER.parent / "deploy_runtime.sh"
COMPOSE = RUNNER.parents[1] / "compose.yaml"


def test_acceptance_runner_requires_clean_tree_and_running_revision_parity():
    source = RUNNER.read_text(encoding="utf-8")

    assert "ALLOW_DIRTY_ACCEPTANCE" in source
    assert "status --porcelain" in source
    assert "working tree is dirty; commit/push before acceptance" in source
    assert "exec -T bot" in source
    assert "exec -T admin" in source
    assert "RUNNING_BOT_REVISION" in source
    assert "RUNNING_ADMIN_REVISION" in source
    assert "ACCEPTANCE_IMAGE_REVISION" in source
    assert "run --rm --no-deps acceptance" in source
    assert "run --rm --no-deps bot" not in source
    assert 'BUILD_REVISION="$HOST_REVISION"' in source
    assert "export BUILD_REVISION" in source
    assert 'build acceptance' in source


def test_acceptance_runner_owns_maintenance_window_and_restores_runtime():
    source = RUNNER.read_text(encoding="utf-8")

    preflight_bot = source.index("RUNNING_BOT_REVISION=$(docker compose")
    preflight_admin = source.index("RUNNING_ADMIN_REVISION=$(docker compose")
    runtime_parity = source.index("revision mismatch before maintenance")
    trap = source.index("trap restore_runtime EXIT HUP INT TERM")
    mark_stopped = source.index("RUNTIME_STOPPED=1", trap)
    stop = source.index('stop bot admin', mark_stopped)
    build = source.index('build acceptance', stop)
    image_revision = source.index("ACCEPTANCE_IMAGE_REVISION=$(", build)
    image_parity = source.index("acceptance image revision mismatch", image_revision)
    acceptance = source.index("-e MINDFLOW_REQUIRE_POSTGRES_TESTS=1", image_parity)
    restore = source.index("\nrestore_runtime\n", acceptance)
    restored_bot = source.index("RESTORED_BOT_REVISION=$(", restore)
    restored_admin = source.index("RESTORED_ADMIN_REVISION=$(", restored_bot)
    restored_parity = source.index("revision mismatch after maintenance", restored_admin)

    assert preflight_bot < preflight_admin < runtime_parity < trap
    assert trap < mark_stopped < stop < build
    assert build < image_revision < image_parity < acceptance < restore
    assert restore < restored_bot < restored_admin < restored_parity

    restore_body = source.split("restore_runtime() {", 1)[1].split("\n}\n", 1)[0]
    assert 'if [ "$RUNTIME_STOPPED" = "1" ]' in restore_body
    assert 'up -d --no-deps bot admin' in restore_body
    assert "RUNTIME_STOPPED=0" in restore_body
    assert "read_restored_revision()" in source
    assert 'while [ "$attempts" -lt 12 ]' in source


def test_acceptance_runner_delegates_test_setup_to_compose_with_postgres_guard():
    source = RUNNER.read_text(encoding="utf-8")

    assert "MINDFLOW_REQUIRE_POSTGRES_TESTS=1" in source
    assert "MINDFLOW_TEST_POSTGRES_URL is required for acceptance" in source
    assert '-e DATABASE_URL="$MINDFLOW_TEST_POSTGRES_URL"' in source
    assert "mindflow_acceptance_test|mindflow_test_*" in source
    assert "refusing acceptance tests outside" in source
    assert "--user root" not in source
    assert "python3 -m pip install" not in source
    assert "pytest==" not in source
    assert "pytest-asyncio==" not in source
    assert '-v "$RUNTIME_ROOT/tests:' not in source
    assert '-v "$PROJECT_ROOT/claude-runtime:' not in source
    assert '-v "$PROJECT_ROOT/docs:' not in source


def test_acceptance_service_is_isolated_from_production_bot_limits_and_state():
    source = COMPOSE.read_text(encoding="utf-8")
    acceptance = source.split("\n  acceptance:\n", 1)[1].split(
        "\n  admin:\n", 1
    )[0]

    assert 'profiles: ["acceptance"]' in acceptance
    assert "context: .." in acceptance
    assert "dockerfile: mindflow-bot-runtime/Dockerfile" in acceptance
    assert "BUILD_REVISION: ${BUILD_REVISION:-development}" in acceptance
    assert "env_file:" in acceptance
    assert "DATABASE_URL: ${MINDFLOW_TEST_POSTGRES_URL:-}" in acceptance
    assert "MINDFLOW_REQUIRE_POSTGRES_TESTS: \"1\"" in acceptance
    assert "postgres:" in acceptance
    assert "migrate:" not in acceptance
    assert "claude-state-init:" not in acceptance
    assert "ports:" not in acceptance
    assert "user: root" in acceptance
    assert "volumes:" in acceptance
    assert "./tests:/srv/runtime/tests:ro" in acceptance
    assert "../docs:/srv/docs:ro" in acceptance
    assert "../claude-runtime:/srv/claude-runtime:ro" in acceptance
    assert "claude_state" not in acceptance
    assert "test -f /srv/runtime/tests/test_postgres_test_guard.py" in acceptance
    assert "test -f /srv/docs/CURRENT_ARCHITECTURE.md" in acceptance
    assert (
        "test -f /srv/claude-runtime/plugins/mindflow-care/skills/"
        "mental-health-care/SKILL.md"
    ) in acceptance
    assert "-r /srv/runtime/requirements-dev.txt" in acceptance
    assert "exec python3 -m pytest -q /srv/runtime/tests" in acceptance
    assert "cpus: ${ACCEPTANCE_CPU_LIMIT:-1.5}" in acceptance
    assert "mem_limit: ${ACCEPTANCE_MEMORY_LIMIT:-1024m}" in acceptance
    assert "memswap_limit: ${ACCEPTANCE_MEMORY_SWAP_LIMIT:-1536m}" in acceptance
    assert "pids_limit: ${ACCEPTANCE_PID_LIMIT:-256}" in acceptance
    assert "${BOT_CPU_LIMIT" not in acceptance
    assert "${BOT_MEMORY_LIMIT" not in acceptance


def test_deploy_runtime_injects_head_migrates_then_recreates_services():
    source = DEPLOY.read_text(encoding="utf-8")

    assert "status --porcelain" in source
    assert "commit/push before deployment" in source
    assert 'BUILD_REVISION=$(git -C "$PROJECT_ROOT" rev-parse HEAD)' in source
    assert "export BUILD_REVISION" in source
    build = source.index('build migrate bot admin')
    postgres = source.index('up -d postgres')
    state_init = source.index('run --rm --no-deps claude-state-init')
    migrate = source.index('run --rm --no-deps migrate')
    recreate = source.index('up -d --no-deps --force-recreate bot admin')
    assert build < postgres < state_init < migrate < recreate
    assert "postgres did not become ready for deployment" in source
    assert "RUNNING_BOT_REVISION" in source
    assert "RUNNING_ADMIN_REVISION" in source
