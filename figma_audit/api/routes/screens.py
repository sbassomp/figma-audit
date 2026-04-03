"""Screen management routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from figma_audit.api.deps import get_project, get_session
from figma_audit.db.models import Project, Screen

router = APIRouter(prefix="/api/projects/{slug}/screens", tags=["screens"])


class ScreenUpdate(BaseModel):
    status: str | None = None  # current | obsolete | draft | component


class MappingUpdate(BaseModel):
    mapped_route: str | None = None
    mapped_page_id: str | None = None


@router.get("")
def list_screens(
    slug: str,
    status: str | None = None,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> list[dict]:
    query = select(Screen).where(Screen.project_id == project.id)
    if status:
        query = query.where(Screen.status == status)
    query = query.order_by(Screen.name)

    screens = session.exec(query).all()
    return [
        {
            "id": s.id,
            "figma_node_id": s.figma_node_id,
            "name": s.name,
            "page": s.page,
            "width": s.width,
            "height": s.height,
            "image_path": s.image_path,
            "status": s.status,
            "mapped_route": s.mapped_route,
            "mapped_page_id": s.mapped_page_id,
            "mapping_confidence": s.mapping_confidence,
        }
        for s in screens
    ]


@router.patch("/{screen_id}")
def update_screen(
    slug: str,
    screen_id: int,
    data: ScreenUpdate,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> dict:
    screen = session.exec(
        select(Screen).where(Screen.id == screen_id, Screen.project_id == project.id)
    ).first()
    if not screen:
        raise HTTPException(status_code=404, detail="Screen not found")

    if data.status:
        if data.status not in ("current", "obsolete", "draft", "component"):
            raise HTTPException(status_code=400, detail="Invalid status")
        screen.status = data.status

    session.add(screen)
    session.commit()
    session.refresh(screen)
    return {"id": screen.id, "name": screen.name, "status": screen.status}


@router.put("/{screen_id}/mapping")
def update_mapping(
    slug: str,
    screen_id: int,
    data: MappingUpdate,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> dict:
    screen = session.exec(
        select(Screen).where(Screen.id == screen_id, Screen.project_id == project.id)
    ).first()
    if not screen:
        raise HTTPException(status_code=404, detail="Screen not found")

    if data.mapped_route is not None:
        screen.mapped_route = data.mapped_route
    if data.mapped_page_id is not None:
        screen.mapped_page_id = data.mapped_page_id

    session.add(screen)
    session.commit()
    session.refresh(screen)
    return {
        "id": screen.id,
        "name": screen.name,
        "mapped_route": screen.mapped_route,
        "mapped_page_id": screen.mapped_page_id,
    }
