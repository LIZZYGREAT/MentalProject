"""SQLAlchemy session lifecycle with no process-global user state."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool


class Base(DeclarativeBase):
    pass


def build_engine(database_url: str, *, echo: bool = False) -> Engine:
    if database_url.startswith("postgresql://"):
        database_url = "postgresql+psycopg://" + database_url[len("postgresql://") :]
    options = {"pool_pre_ping": True, "echo": echo, "future": True}
    if database_url in {"sqlite://", "sqlite:///:memory:"}:
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
        self._sessions = sessionmaker(
            bind=engine,
            class_=Session,
            expire_on_commit=False,
            autoflush=False,
        )

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self._sessions()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def create_schema_for_tests(self) -> None:
        """Tests only; production schema is managed by Alembic."""

        from app import models  # noqa: F401

        Base.metadata.create_all(self.engine)
