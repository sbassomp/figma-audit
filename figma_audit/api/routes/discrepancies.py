"""Discrepancy management routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from figma_audit.api.deps import get_project, get_session
from figma_audit.db.models import Annotation, Discrepancy, Project, Run

router = APIRouter(prefix="/api/projects/{slug}", tags=["discrepancies"])


class DiscrepancyUpdate(BaseModel):
    status: str  # open | ignored | acknowledged | fixed | wontfix


class AnnotationCreate(BaseModel):
    content: str
    author: str = "user"


@router.get("/runs/{run_id}/discrepancies")
def list_discrepancies(
    slug: str,
    run_id: int,
    severity: str | None = None,
    category: str | None = None,
    status: str | None = None,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> list[dict]:
    run = session.exec(select(Run).where(Run.id == run_id, Run.project_id == project.id)).first()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    query = select(Discrepancy).where(Discrepancy.run_id == run_id)
    if severity:
        query = query.where(Discrepancy.severity == severity)
    if category:
        query = query.where(Discrepancy.category == category)
    if status:
        query = query.where(Discrepancy.status == status)
    query = query.order_by(Discrepancy.severity, Discrepancy.category)

    discrepancies = session.exec(query).all()
    return [
        {
            "id": d.id,
            "page_id": d.page_id,
            "route": d.route,
            "category": d.category,
            "severity": d.severity,
            "description": d.description,
            "figma_value": d.figma_value,
            "app_value": d.app_value,
            "location": d.location,
            "status": d.status,
            "overall_fidelity": d.overall_fidelity,
        }
        for d in discrepancies
    ]


@router.patch("/discrepancies/{disc_id}")
def update_discrepancy(
    slug: str,
    disc_id: int,
    data: DiscrepancyUpdate,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> dict:
    disc = session.get(Discrepancy, disc_id)
    if not disc:
        raise HTTPException(status_code=404, detail="Discrepancy not found")

    valid_statuses = ("open", "ignored", "acknowledged", "fixed", "wontfix")
    if data.status not in valid_statuses:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid status. Must be one of: {valid_statuses}",
        )

    disc.status = data.status
    session.add(disc)
    session.commit()
    session.refresh(disc)
    return {"id": disc.id, "status": disc.status}


@router.post("/discrepancies/{disc_id}/annotate", status_code=201)
def annotate_discrepancy(
    slug: str,
    disc_id: int,
    data: AnnotationCreate,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> dict:
    disc = session.get(Discrepancy, disc_id)
    if not disc:
        raise HTTPException(status_code=404, detail="Discrepancy not found")

    annotation = Annotation(
        discrepancy_id=disc_id,
        author=data.author,
        content=data.content,
    )
    session.add(annotation)
    session.commit()
    session.refresh(annotation)
    return {"id": annotation.id, "content": annotation.content, "author": annotation.author}


@router.get("/discrepancies/{disc_id}/annotations")
def list_annotations(
    slug: str,
    disc_id: int,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> list[dict]:
    annotations = session.exec(
        select(Annotation)
        .where(Annotation.discrepancy_id == disc_id)
        .order_by(Annotation.created_at)
    ).all()
    return [
        {
            "id": a.id,
            "author": a.author,
            "content": a.content,
            "created_at": a.created_at.isoformat(),
        }
        for a in annotations
    ]
