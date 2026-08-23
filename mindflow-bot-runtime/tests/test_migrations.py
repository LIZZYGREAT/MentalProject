from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


VERSIONS = Path(__file__).resolve().parents[1] / "migrations" / "versions"
VERSION_NUM_CAPACITY = 64


def _migration(path: Path):
    spec = spec_from_file_location(f"test_migration_{path.stem}", path)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_0008_expands_alembic_version_before_business_schema(monkeypatch):
    migration = _migration(VERSIONS / "0008_forecast_observation_revision.py")
    statements = []
    monkeypatch.setattr(migration.op, "execute", statements.append)

    migration.upgrade()

    assert statements[0] == (
        "ALTER TABLE alembic_version "
        "ALTER COLUMN version_num TYPE VARCHAR(64)"
    )
    assert "ADD COLUMN observation_revision VARCHAR(64)" in statements[1]
    assert all("TYPE VARCHAR(32)" not in statement for statement in statements)

    statements.clear()
    migration.downgrade()
    assert all("alembic_version" not in statement for statement in statements)


def test_migration_revision_ids_fit_alembic_version_capacity():
    migrations = [_migration(path) for path in sorted(VERSIONS.glob("*.py"))]
    revisions = [migration.revision for migration in migrations]

    assert max(map(len, revisions)) <= VERSION_NUM_CAPACITY
    assert "0008_forecast_observation_revision" in revisions
    migration_0009 = next(
        migration for migration in migrations
        if migration.revision == "0009_learned_model_profiles"
    )
    assert migration_0009.down_revision == "0008_forecast_observation_revision"
    migration_0010 = next(
        migration for migration in migrations
        if migration.revision == "0010_response_delivery"
    )
    assert migration_0010.down_revision == "0009_learned_model_profiles"
    assert len(migration_0010.revision) <= 32
