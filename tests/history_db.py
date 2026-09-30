"""SQLite sessions for history tests.

Postgres ARRAY and JSONB columns are rendered as JSON for the fixture and
restored afterward. Production models stay on the Postgres types.
"""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import JSON, create_engine, event
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.sql.schema import Column
from sqlalchemy.types import TypeEngine

from music_discovery.db import Base
from music_discovery.spotify.auth import SessionFactory


@contextmanager
def _sqlite_engine() -> Iterator[Engine]:
    originals: list[tuple[Column[object], TypeEngine[object]]] = []
    for table in Base.metadata.tables.values():
        for column in table.columns:
            if isinstance(column.type, (ARRAY, JSONB)):
                originals.append((column, column.type))
                column.type = JSON()
    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _foreign_keys(dbapi_connection, connection_record) -> None:
        del connection_record
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    try:
        Base.metadata.create_all(engine)
        yield engine
    finally:
        for column, column_type in originals:
            column.type = column_type


@contextmanager
def history_session() -> Iterator[Session]:
    with _sqlite_engine() as engine:
        session = Session(engine)
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


@contextmanager
def history_sessions() -> Iterator[SessionFactory]:
    with _sqlite_engine() as engine:
        factory = sessionmaker(engine, expire_on_commit=False)

        @contextmanager
        def sessions() -> Iterator[Session]:
            session = factory()
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()

        yield sessions
