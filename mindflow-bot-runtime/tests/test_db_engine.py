from unittest.mock import Mock

from sqlalchemy.pool import StaticPool

from app import db


def test_postgres_build_engine_adds_explicit_connect_timeout(monkeypatch):
    engine = Mock()
    create_engine = Mock(return_value=engine)
    monkeypatch.setattr(db, "create_engine", create_engine)

    result = db.build_engine(
        "postgresql://tester:secret@localhost/mindflow_test_ci",
        connect_timeout_seconds=5,
    )

    assert result is engine
    create_engine.assert_called_once_with(
        "postgresql+psycopg://tester:secret@localhost/mindflow_test_ci",
        pool_pre_ping=True,
        echo=False,
        future=True,
        connect_args={"connect_timeout": 5},
    )


def test_postgres_build_engine_default_does_not_force_connect_timeout(monkeypatch):
    create_engine = Mock(return_value=Mock())
    monkeypatch.setattr(db, "create_engine", create_engine)

    db.build_engine("postgresql+psycopg://tester:secret@localhost/mindflow")

    assert "connect_args" not in create_engine.call_args.kwargs


def test_sqlite_in_memory_engine_keeps_static_pool_topology(monkeypatch):
    create_engine = Mock(return_value=Mock())
    monkeypatch.setattr(db, "create_engine", create_engine)

    db.build_engine("sqlite:///:memory:")

    assert create_engine.call_args.kwargs["connect_args"] == {
        "check_same_thread": False
    }
    assert create_engine.call_args.kwargs["poolclass"] is StaticPool
