"""SQLite sessions for history tests.

Postgres ARRAY and JSONB columns are rendered as JSON for the fixture and
restored afterward. Production models stay on the Postgres types.
"""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import JSON, create_engine, event
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from sqlalchemy.sql.schema import Column
from sqlalchemy.types import TypeEngine

from music_discovery.db import Base


@contextmanager
def history_session() -> Iterator[Session]:
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

    session: Session | None = None
    try:
        Base.metadata.create_all(engine)
        session = Session(engine)
        yield session
        session.commit()
    except Exception:
        if session is not None:
            session.rollback()
        raise
    finally:
        if session is not None:
            session.close()
        for column, column_type in originals:
            column.type = column_type
