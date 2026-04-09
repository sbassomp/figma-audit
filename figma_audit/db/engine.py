"""Database engine and session management."""

from __future__ import annotations

from collections.abc import Generator

from sqlmodel import Session, SQLModel, create_engine

_engine = None
_engine_path: str | None = None
_default_db_path: str = "figma-audit.db"


def set_default_db_path(path: str) -> None:
    """Set the default DB path used when get_engine() is called without arguments."""
    global _default_db_path
    _default_db_path = path


def get_engine(db_path: str | None = None):
    """Get or create the SQLite engine."""
    global _engine, _engine_path
    if db_path is None:
        db_path = _default_db_path
    if _engine is None or _engine_path != db_path:
        url = f"sqlite:///{db_path}"
        _engine = create_engine(url, echo=False)
        _engine_path = db_path
    return _engine


def init_db(db_path: str | None = None) -> None:
    """Create all tables if they don't exist, then apply lightweight migrations."""
    from sqlalchemy import text

    from figma_audit.db.models import (  # noqa: F401 — ensure models are registered
        Annotation,
        Capture,
        Discrepancy,
        Project,
        Run,
        Screen,
    )

    engine = get_engine(db_path)
    SQLModel.metadata.create_all(engine)

    # Lightweight schema migrations: add new columns to existing tables.
    # SQLite ALTER TABLE ADD COLUMN is idempotent-via-catch: if the column
    # already exists, it raises and we swallow the error.
    migrations = [
        ("capture", "landed_url", "VARCHAR"),
    ]
    with engine.begin() as conn:
        for table, column, col_type in migrations:
            try:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}"))
            except Exception:
                pass  # Column already exists


def get_session(db_path: str | None = None) -> Generator[Session, None, None]:
    """Yield a database session."""
    engine = get_engine(db_path)
    with Session(engine) as session:
        yield session
