"""Project CRUD routes."""

from __future__ import annotations

import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from figma_audit.api.deps import get_session
from figma_audit.db.models import Project, Run

router = APIRouter(prefix="/api/projects", tags=["projects"])


class ProjectCreate(BaseModel):
    name: str
    figma_url: str | None = None
    app_url: str | None = None
    project_path: str | None = None
    output_dir: str = "./output"


class ProjectUpdate(BaseModel):
    name: str | None = None
    figma_url: str | None = None
    app_url: str | None = None
    project_path: str | None = None
    output_dir: str | None = None


def _slugify(name: str) -> str:
    s = name.lower().strip()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_]+", "-", s)
    return re.sub(r"-+", "-", s).strip("-")


@router.get("")
def list_projects(session: Session = Depends(get_session)) -> list[dict]:
    projects = session.exec(select(Project).order_by(Project.updated_at.desc())).all()
    result = []
    for p in projects:
        last_run = session.exec(
            select(Run).where(Run.project_id == p.id).order_by(Run.created_at.desc())
        ).first()
        result.append({
            "id": p.id,
            "name": p.name,
            "slug": p.slug,
            "figma_url": p.figma_url,
            "app_url": p.app_url,
            "output_dir": p.output_dir,
            "created_at": p.created_at.isoformat(),
            "last_run": {
                "id": last_run.id,
                "status": last_run.status,
                "created_at": last_run.created_at.isoformat(),
            } if last_run else None,
        })
    return result


@router.post("", status_code=201)
def create_project(data: ProjectCreate, session: Session = Depends(get_session)) -> dict:
    slug = _slugify(data.name)
    existing = session.exec(select(Project).where(Project.slug == slug)).first()
    if existing:
        raise HTTPException(status_code=409, detail=f"Project '{slug}' already exists")

    project = Project(
        name=data.name,
        slug=slug,
        figma_url=data.figma_url,
        app_url=data.app_url,
        project_path=data.project_path,
        output_dir=data.output_dir,
    )
    session.add(project)
    session.commit()
    session.refresh(project)
    return {"id": project.id, "slug": project.slug, "name": project.name}


@router.get("/{slug}")
def get_project(slug: str, session: Session = Depends(get_session)) -> dict:
    project = session.exec(select(Project).where(Project.slug == slug)).first()
    if not project:
        raise HTTPException(status_code=404, detail=f"Project '{slug}' not found")

    runs = session.exec(
        select(Run).where(Run.project_id == project.id).order_by(Run.created_at.desc()).limit(20)
    ).all()

    return {
        "id": project.id,
        "name": project.name,
        "slug": project.slug,
        "figma_url": project.figma_url,
        "app_url": project.app_url,
        "project_path": project.project_path,
        "output_dir": project.output_dir,
        "created_at": project.created_at.isoformat(),
        "updated_at": project.updated_at.isoformat(),
        "runs": [
            {
                "id": r.id,
                "status": r.status,
                "current_phase": r.current_phase,
                "started_at": r.started_at.isoformat() if r.started_at else None,
                "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                "stats_json": r.stats_json,
                "error": r.error,
                "created_at": r.created_at.isoformat(),
            }
            for r in runs
        ],
    }


@router.put("/{slug}")
def update_project(
    slug: str, data: ProjectUpdate, session: Session = Depends(get_session)
) -> dict:
    project = session.exec(select(Project).where(Project.slug == slug)).first()
    if not project:
        raise HTTPException(status_code=404, detail=f"Project '{slug}' not found")

    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(project, field, value)
    project.updated_at = datetime.now(timezone.utc)

    session.add(project)
    session.commit()
    session.refresh(project)
    return {"id": project.id, "slug": project.slug, "name": project.name}


@router.delete("/{slug}", status_code=204)
def delete_project(slug: str, session: Session = Depends(get_session)) -> None:
    project = session.exec(select(Project).where(Project.slug == slug)).first()
    if not project:
        raise HTTPException(status_code=404, detail=f"Project '{slug}' not found")
    session.delete(project)
    session.commit()
