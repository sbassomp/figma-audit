"""FastAPI dependency injection."""

from __future__ import annotations

from collections.abc import Generator

from fastapi import Depends, HTTPException
from sqlmodel import Session, select

from figma_audit.db.engine import get_session as _get_session
from figma_audit.db.models import Project

# Database path — set by the app factory
_db_path: str = "figma-audit.db"


def set_db_path(path: str) -> None:
    global _db_path
    _db_path = path


def get_session() -> Generator[Session, None, None]:
    yield from _get_session(_db_path)


def get_project(slug: str, session: Session = Depends(get_session)) -> Project:
    project = session.exec(select(Project).where(Project.slug == slug)).first()
    if not project:
        raise HTTPException(status_code=404, detail=f"Project '{slug}' not found")
    return project
