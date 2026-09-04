from pathlib import Path


RUNTIME_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = RUNTIME_ROOT / "scripts"
COMMON = SCRIPTS / "acceptance_common.sh"
PREPARE = SCRIPTS / "prepare_acceptance_image.sh"
RUNNER = SCRIPTS / "run_acceptance_tests.sh"
DEPLOY = SCRIPTS / "deploy_runtime.sh"
COMPOSE = RUNTIME_ROOT / "compose.yaml"
DOCKERFILE = RUNTIME_ROOT / "Dockerfile"
DEV_REQUIREMENTS = RUNTIME_ROOT / "requirements-dev.txt"
ENV_EXAMPLE = RUNTIME_ROOT / ".env.example"


def test_common_preflight_checks_docker_builder_recent_fatals_and_resources():
    source = COMMON.read_text(encoding="utf-8")

    assert "docker info" in source
    assert "docker buildx inspect default" in source
    assert "Status:[[:space:]]*running" in source
    assert 'journalctl -u docker -b --since "10 minutes ago" --no-pager' in source
    assert "only one connection allowed" in source
    assert "healthcheck failed fatally" in source
    assert "session healthcheck failed fatally" in source
    assert "Docker/BuildKit unhealthy; refusing acceptance operation" in source
    assert "ACCEPTANCE_MIN_FREE_DISK_MB:-10240" in source
    assert "df -Pk /" in source
    assert "ACCEPTANCE_MIN_AVAILABLE_MEMORY_MB:-512" in source
    assert "/^MemAvailable:/" in source
    assert "MemFree" not in source


def test_common_reads_acceptance_revision_from_image_metadata():
    source = COMMON.read_text(encoding="utf-8")

    assert "docker image inspect" in source
    assert "BUILD_REVISION=" in source
    assert "require_current_acceptance_image" in source
    assert "acceptance image missing or stale" in source
    assert "prepare_acceptance_image.sh first" in source
    image_helper = source.split("load_acceptance_image_revision()", 1)[1].split(
        "\n}", 1
    )[0]
    assert "run --rm" not in image_helper


def test_common_provides_cache_cap_and_lightweight_postgres_validation():
    source = COMMON.read_text(encoding="utf-8")

    assert "cap_acceptance_build_cache()" in source
    assert "ACCEPTANCE_BUILD_CACHE_KEEP:-6GB" in source
    assert "docker builder prune -a -f" in source
    assert '--keep-storage "$ACCEPTANCE_BUILD_CACHE_KEEP"' in source
    assert "docker system prune" not in source
    assert "volume prune" not in source

    validator = source.split(
        "validate_postgres_target_with_acceptance_image()", 1
    )[1].split("\n}", 1)[0]
    assert "run --rm --no-deps" in validator
    assert "python3 -m app.postgres_test_guard" in validator
    assert "pytest" not in validator


def test_prepare_runner_orders_preflight_trap_stop_and_build():
    source = PREPARE.read_text(encoding="utf-8")

    clean = source.index("require_clean_working_tree")
    revision = source.index("load_host_revision", clean)
    preflight = source.index("acceptance_preflight", revision)
    running_parity = source.index("require_running_revision_parity", preflight)
    trap = source.index("trap prepare_cleanup EXIT", running_parity)
    mark_stopped = source.index("RUNTIME_STOPPED=1", trap)
    stop = source.index("stop bot admin", mark_stopped)
    build_started = source.index("BUILD_STARTED=1", stop)
    build = source.index("build acceptance", build_started)
    image_parity = source.index("acceptance image revision mismatch", build)
    restore = source.index("\nrestore_runtime\n", image_parity)
    restored_parity = source.index("require_restored_revision_parity", restore)

    assert clean < revision < preflight < running_parity < trap
    assert trap < mark_stopped < stop < build_started < build < image_parity
    assert image_parity < restore < restored_parity


def test_prepare_exit_cleanup_restores_then_caps_failed_build_cache_and_preserves_status():
    source = PREPARE.read_text(encoding="utf-8")
    cleanup = source.split("prepare_cleanup() {", 1)[1].split("\n}", 1)[0]

    save_status = cleanup.index("original_status=$?")
    disable_traps = cleanup.index("trap - EXIT HUP INT TERM", save_status)
    restore = cleanup.index("restore_runtime", disable_traps)
    build_guard = cleanup.index('BUILD_STARTED:-0}" = "1"', restore)
    cache_cap = cleanup.index("cap_acceptance_build_cache", build_guard)
    docker_df = cleanup.index("docker system df", cache_cap)
    root_df = cleanup.index("df -h /", docker_df)
    docker_ps = cleanup.index("docker ps", root_df)
    preserved_exit = cleanup.index('exit "$original_status"', docker_ps)

    assert save_status < disable_traps < restore < build_guard < cache_cap
    assert cache_cap < docker_df < root_df < docker_ps < preserved_exit
    assert "BUILD_STARTED=1" in source
    assert "cap_acceptance_build_cache || true" in cleanup
    assert "trap 'exit 129' HUP" in source
    assert "trap 'exit 130' INT" in source
    assert "trap 'exit 143' TERM" in source


def test_full_runner_never_builds_and_restores_after_running_acceptance():
    source = RUNNER.read_text(encoding="utf-8")

    assert "build acceptance" not in source
    assert "python3 -m pip install" not in source
    assert "require_clean_working_tree" in source
    assert "require_running_revision_parity" in source

    preflight = source.index("acceptance_preflight")
    image_gate = source.index("require_current_acceptance_image", preflight)
    postgres_validation = source.index(
        "validate_postgres_target_with_acceptance_image", image_gate
    )
    trap = source.index("trap restore_runtime EXIT HUP INT TERM", postgres_validation)
    stop = source.index("stop bot admin", trap)
    acceptance = source.index("run --rm --no-deps", stop)
    restore = source.index("\nrestore_runtime\n", acceptance)
    restored_parity = source.index("require_restored_revision_parity", restore)

    assert preflight < image_gate < postgres_validation < trap
    assert trap < stop < acceptance < restore < restored_parity
    assert '-e DATABASE_URL="$MINDFLOW_TEST_POSTGRES_URL"' in source
    assert "MINDFLOW_REQUIRE_POSTGRES_TESTS=1" in source


def test_acceptance_image_bakes_dev_dependencies_and_runtime_only_runs_pytest():
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    dev_requirements = DEV_REQUIREMENTS.read_text(encoding="utf-8")
    compose = COMPOSE.read_text(encoding="utf-8")
    acceptance = compose.split("\n  acceptance:\n", 1)[1].split("\n  admin:\n", 1)[0]

    assert "ARG INSTALL_DEV_DEPS=0" in dockerfile
    assert "requirements-dev.txt" in dockerfile
    assert "pytest==8.4.1" in dev_requirements
    assert "pytest-asyncio==1.1.0" in dev_requirements
    assert 'INSTALL_DEV_DEPS: "1"' in acceptance
    assert "python3 -m pip install" not in acceptance
    assert "apt-get" not in acceptance
    assert "exec python3 -m pytest -q /srv/runtime/tests" in acceptance
    assert "test -f /srv/runtime/tests/test_postgres_test_guard.py" in acceptance
    assert "test -f /srv/docs/CURRENT_ARCHITECTURE.md" in acceptance
    assert (
        "test -f /srv/claude-runtime/plugins/mindflow-care/skills/"
        "mental-health-care/SKILL.md"
    ) in acceptance
    assert "cpus: ${ACCEPTANCE_CPU_LIMIT:-1.5}" in acceptance
    assert "mem_limit: ${ACCEPTANCE_MEMORY_LIMIT:-1024m}" in acceptance
    assert "memswap_limit: ${ACCEPTANCE_MEMORY_SWAP_LIMIT:-1536m}" in acceptance
    assert "pids_limit: ${ACCEPTANCE_PID_LIMIT:-256}" in acceptance


def test_acceptance_environment_example_documents_safety_controls():
    source = ENV_EXAMPLE.read_text(encoding="utf-8")

    assert "ACCEPTANCE_MIN_FREE_DISK_MB=10240" in source
    assert "ACCEPTANCE_MIN_AVAILABLE_MEMORY_MB=512" in source
    assert "ACCEPTANCE_BUILD_CACHE_KEEP=6GB" in source
    assert "ACCEPTANCE_CPU_LIMIT=1.5" in source
    assert "ACCEPTANCE_MEMORY_LIMIT=1024m" in source
    assert "ACCEPTANCE_MEMORY_SWAP_LIMIT=1536m" in source
    assert "ACCEPTANCE_PID_LIMIT=256" in source
    assert (
        "MINDFLOW_TEST_POSTGRES_ALLOWED_HOSTS=postgres,localhost,127.0.0.1,::1"
        in source
    )
    assert "MINDFLOW_TEST_POSTGRES_CONNECT_TIMEOUT_SECONDS=5" in source
    assert "postgres_data" in source
    assert "production DATABASE_URL" in source


def test_deploy_runtime_injects_head_migrates_then_recreates_services():
    source = DEPLOY.read_text(encoding="utf-8")

    assert "status --porcelain" in source
    assert "commit/push before deployment" in source
    assert 'BUILD_REVISION=$(git -C "$PROJECT_ROOT" rev-parse HEAD)' in source
    assert "export BUILD_REVISION" in source
    build = source.index("build migrate bot admin")
    postgres = source.index("up -d postgres")
    state_init = source.index("run --rm --no-deps claude-state-init")
    migrate = source.index("run --rm --no-deps migrate")
    recreate = source.index("up -d --no-deps --force-recreate bot admin")
    assert build < postgres < state_init < migrate < recreate
    assert "postgres did not become ready for deployment" in source
    assert "RUNNING_BOT_REVISION" in source
    assert "RUNNING_ADMIN_REVISION" in source
