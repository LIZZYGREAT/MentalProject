from pathlib import Path


RUNNER = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "run_acceptance_tests.sh"
)
DEPLOY = RUNNER.parent / "deploy_runtime.sh"


def test_acceptance_runner_requires_clean_tree_and_running_revision_parity():
    source = RUNNER.read_text(encoding="utf-8")

    assert "ALLOW_DIRTY_ACCEPTANCE" in source
    assert "status --porcelain" in source
    assert "working tree is dirty; commit/push before acceptance" in source
    assert "exec -T bot" in source
    assert "exec -T admin" in source
    assert "RUNNING_BOT_REVISION" in source
    assert "RUNNING_ADMIN_REVISION" in source
    assert "docker compose -f \"$COMPOSE_FILE\" build" not in source


def test_acceptance_runner_checks_mounts_and_runs_full_suite_with_postgres_guard():
    source = RUNNER.read_text(encoding="utf-8")

    assert "test -f /srv/docs/CURRENT_ARCHITECTURE.md" in source
    assert (
        "test -f /srv/claude-runtime/plugins/mindflow-care/skills/"
        "mental-health-care/SKILL.md"
    ) in source
    assert "MINDFLOW_REQUIRE_POSTGRES_TESTS=1" in source
    assert "--user root" in source
    assert "pytest==8.4.1" in source
    assert "pytest-asyncio==1.1.0" in source
    assert "exec python3 -m pytest -q tests" in source


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
