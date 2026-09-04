from pathlib import Path


RUNNER = Path(__file__).resolve().parents[1] / "scripts" / "run_admin_tests.sh"


def test_admin_runner_is_lightweight_revision_gated_and_non_destructive():
    source = RUNNER.read_text(encoding="utf-8")

    for test_file in (
        "test_admin_daily_review_dependency_refresh.py",
        "test_admin_forecast_visualization.py",
        "test_admin_security.py",
        "test_admin_visualization_contract.py",
        "test_admin_web.py",
    ):
        assert f"/srv/runtime/tests/{test_file}" in source

    assert "require_current_acceptance_image" in source
    assert "run --rm --no-deps" in source
    assert "MINDFLOW_REQUIRE_POSTGRES_TESTS=0" in source
    assert "docker inspect" in source
    assert "stop bot admin" not in source
    assert "build acceptance" not in source
    assert "MINDFLOW_REQUIRE_POSTGRES_TESTS=1" not in source
    assert "pip install" not in source
    assert "docker compose down" not in source
    assert "docker system prune" not in source
    assert "volume prune" not in source
    assert "up -d" not in source
