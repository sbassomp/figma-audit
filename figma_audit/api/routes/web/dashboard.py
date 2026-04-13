"""Dashboard page (the ``/`` landing route)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlmodel import Session, func, select

from figma_audit.api.deps import get_session
from figma_audit.api.routes.web._state import _nav_projects, templates
from figma_audit.db.models import Discrepancy, Project, Run

router = APIRouter(tags=["web"])


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, session: Session = Depends(get_session)):
    projects = session.exec(select(Project).order_by(Project.updated_at.desc())).all()

    project_list = []
    total_runs = 0
    total_critical = 0
    total_fixed = 0

    for p in projects:
        last_run = session.exec(
            select(Run).where(Run.project_id == p.id).order_by(Run.created_at.desc())
        ).first()

        critical_count = session.exec(
            select(func.count(Discrepancy.id))
            .join(Run)
            .where(
                Run.project_id == p.id,
                Discrepancy.severity == "critical",
                Discrepancy.status == "open",
            )
        ).one()

        fixed_count = session.exec(
            select(func.count(Discrepancy.id))
            .join(Run)
            .where(
                Run.project_id == p.id,
                Discrepancy.status == "fixed",
            )
        ).one()

        run_count = session.exec(
            select(func.count(Run.id)).where(Run.project_id == p.id)
        ).one()

        total_runs += run_count
        total_critical += critical_count
        total_fixed += fixed_count

        project_list.append(
            {
                "name": p.name,
                "slug": p.slug,
                "app_url": p.app_url,
                "last_run_date": last_run.created_at.strftime("%Y-%m-%d %H:%M")
                if last_run
                else None,
                "last_run_status": last_run.status if last_run else None,
            }
        )

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        context={
            "active_nav": "dashboard",
            "nav_projects": _nav_projects(session),
            "projects": project_list,
            "total_runs": total_runs,
            "total_critical": total_critical,
            "total_fixed": total_fixed,
        },
    )
