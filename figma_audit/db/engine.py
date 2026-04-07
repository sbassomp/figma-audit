"""Database engine and session management."""

from __future__ import annotations

from collections.abc import Generator

from sqlmodel import Session, SQLModel, create_engine

_engine = None
_engine_path: str | None = None


def get_engine(db_path: str = "figma-audit.db"):
    """Get or create the SQLite engine."""
    global _engine, _engine_path
    if _engine is None or _engine_path != db_path:
        url = f"sqlite:///{db_path}"
        _engine = create_engine(url, echo=False)
        _engine_path = db_path
    return _engine


def init_db(db_path: str = "figma-audit.db") -> None:
    """Create all tables if they don't exist."""
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


def get_session(db_path: str = "figma-audit.db") -> Generator[Session, None, None]:
    """Yield a database session."""
    engine = get_engine(db_path)
    with Session(engine) as session:
        yield session
