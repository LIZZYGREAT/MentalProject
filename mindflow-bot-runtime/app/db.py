"""SQLAlchemy session lifecycle with no process-global user state."""

from __future__ import annotations

from contextlib import contextmanager
import threading
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool


class Base(DeclarativeBase):
    pass


def build_engine(
    database_url: str,
    *,
    echo: bool = False,
    connect_timeout_seconds: int | None = None,
) -> Engine:
    if database_url.startswith("postgresql://"):
        database_url = "postgresql+psycopg://" + database_url[len("postgresql://") :]
    options = {"pool_pre_ping": True, "echo": echo, "future": True}
    if database_url.startswith("postgresql+") and connect_timeout_seconds is not None:
        options["connect_args"] = {"connect_timeout": connect_timeout_seconds}
    elif database_url in {"sqlite://", "sqlite:///:memory:"}:
        options.update(
            {
                "connect_args": {"check_same_thread": False},
                "poolclass": StaticPool,
            }
        )
    return create_engine(database_url, **options)


class Database:
    def __init__(self, engine: Engine):
        self.engine = engine
        # In-memory SQLite under StaticPool is one physical connection shared
        # by every worker thread. Concurrent Session transactions on that one
        # connection corrupt each other's unit-of-work state, so serialize
        # only this test/local topology. PostgreSQL and file-backed pooled
        # databases retain normal connection-level concurrency.
        self._single_connection_lock = (
            threading.RLock() if isinstance(engine.pool, StaticPool) else None
        )
        self._sessions = sessionmaker(
            bind=engine,
            class_=Session,
            expire_on_commit=False,
            autoflush=False,
        )

    @contextmanager
    def session(self) -> Iterator[Session]:
        lock = self._single_connection_lock
        if lock is not None:
            lock.acquire()
        session: Session | None = None
        try:
            session = self._sessions()
            yield session
            session.commit()
        except Exception:
            if session is not None:
                session.rollback()
            raise
        finally:
            if session is not None:
                session.close()
            if lock is not None:
                lock.release()

    def create_schema_for_tests(self) -> None:
        """Tests only; production schema is managed by Alembic."""

        from app import models  # noqa: F401

        Base.metadata.create_all(self.engine)
