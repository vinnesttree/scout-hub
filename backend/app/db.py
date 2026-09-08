"""Database engine + session helpers."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlmodel import Session, SQLModel, create_engine

from app.config import get_settings

_settings = get_settings()

engine = create_engine(
    _settings.db_url,
    echo=False,
    connect_args={"check_same_thread": False},
)


def init_db() -> None:
    # Import side effect: register all tables on SQLModel.metadata.
    from app import models  # noqa: F401

    SQLModel.metadata.create_all(engine)
    _ensure_columns()


# Additive SQLite migrations for columns introduced after a table shipped.
# (No prod deployments to worry about; this just spares dev DBs a manual drop.)
_ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "recording": {
        "is_live": "BOOLEAN",
        "sections": "VARCHAR",
    },
}


def _ensure_columns() -> None:
    with engine.begin() as conn:
        for table, cols in _ADDED_COLUMNS.items():
            existing = {
                row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")
            }
            for name, ddl in cols.items():
                if name not in existing:
                    conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def get_session() -> Iterator[Session]:
    """FastAPI dependency."""
    with Session(engine) as session:
        yield session


@contextmanager
def session_scope() -> Iterator[Session]:
    """Standalone context manager for background jobs."""
    session = Session(engine)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
