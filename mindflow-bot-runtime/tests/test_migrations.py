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
    migration_0014 = next(
        migration for migration in migrations
        if migration.revision == "0014_daily_review_expiry"
    )
    assert migration_0014.down_revision == "0013_daily_review_feedback"
    migration_0015 = next(
        migration for migration in migrations
        if migration.revision == "0015_daily_review_causal_source"
    )
    assert migration_0015.down_revision == "0014_daily_review_expiry"


def test_0015_backfills_causal_source_without_guessing_orphan_responses(
    monkeypatch,
):
    migration = _migration(
        VERSIONS / "0015_daily_review_causal_source.py"
    )
    columns = []
    foreign_keys = []
    statements = []
    monkeypatch.setattr(
        migration.op,
        "add_column",
        lambda table, column: columns.append((table, column)),
    )
    monkeypatch.setattr(
        migration.op,
        "create_foreign_key",
        lambda *args, **kwargs: foreign_keys.append((args, kwargs)),
    )
    monkeypatch.setattr(migration.op, "execute", statements.append)

    migration.upgrade()

    assert [column.name for _, column in columns] == [
        "causal_source_forecast_id",
        "causal_source_forecast_version",
    ]
    assert all(column.nullable for _, column in columns)
    assert foreign_keys[0][1]["ondelete"] == "RESTRICT"
    statement = statements[0]
    assert "DISTINCT ON (daily_review_response_id)" in statement
    assert "generated_at ASC, id ASC" in statement
    assert "response.causal_source_forecast_id IS NULL" in statement
