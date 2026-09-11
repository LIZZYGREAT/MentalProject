from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
CLEANUP = SCRIPTS / "clean_stale_containers.sh"


def test_cleanup_keeps_builder_cache_by_default_and_requires_explicit_opt_in():
    source = CLEANUP.read_text(encoding="utf-8")

    assert "[--dry-run] [--build-cache]" in source
    assert "PRUNE_BUILD_CACHE=0" in source
    assert "--build-cache) PRUNE_BUILD_CACHE=1" in source
    assert 'if [ "$PRUNE_BUILD_CACHE" = "1" ]; then' in source
    assert 'docker builder prune -a -f --keep-storage "$ACCEPTANCE_BUILD_CACHE_KEEP"' in source
    assert "this can evict the pip download cache" in source
