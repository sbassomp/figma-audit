"""Database engine and session management."""

from __future__ import annotations

from pathlib import Path
from collections.abc import Generator

from sqlmodel import Session, SQLModel, create_engine

_engine = None


def get_engine(db_path: str = "figma-audit.db"):
    """Get or create the SQLite engine."""
    global _engine
    if _engine is None:
        url = f"sqlite:///{db_path}"
        _engine = create_engine(url, echo=False)
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
