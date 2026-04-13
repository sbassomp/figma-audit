"""Project CRUD pages: create form, project detail, screens gallery."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session, func, select

from figma_audit.api.deps import get_session
from figma_audit.api.routes.web._state import _nav_projects, templates
from figma_audit.db.models import Project, Run, Screen

router = APIRouter(tags=["web"])


@router.get("/projects/new", response_class=HTMLResponse)
def new_project_form(request: Request, session: Session = Depends(get_session)):
    return templates.TemplateResponse(
        request,
        "new_project.html",
        context={
            "active_nav": "new_project",
            "nav_projects": _nav_projects(session),
        },
    )


@router.post("/projects/new")
def create_project_form(
    name: str = Form(...),
    figma_url: str = Form(""),
    app_url: str = Form(""),
    project_path: str = Form(""),
    output_dir: str = Form("./output"),
    test_email: str = Form(""),
    test_otp: str = Form("1234"),
    seed_email: str = Form(""),
    seed_otp: str = Form("1234"),
    session: Session = Depends(get_session),
):
    import re

    slug = re.sub(r"[^\w\s-]", "", name.lower().strip())
    slug = re.sub(r"[\s_]+", "-", slug).strip("-")

    project = Project(
        name=name,
        slug=slug,
        figma_url=figma_url or None,
        app_url=app_url or None,
        project_path=project_path or None,
        output_dir=output_dir,
        test_email=test_email or None,
        test_otp=test_otp,
        seed_email=seed_email or None,
        seed_otp=seed_otp,
    )
    session.add(project)
    session.commit()
    return RedirectResponse(f"/projects/{slug}", status_code=303)


@router.get("/projects/{slug}", response_class=HTMLResponse)
def project_detail(request: Request, slug: str, session: Session = Depends(get_session)):
    project = session.exec(select(Project).where(Project.slug == slug)).first()
    if not project:
        return RedirectResponse("/")

    runs = session.exec(
        select(Run).where(Run.project_id == project.id).order_by(Run.created_at.desc())
    ).all()

    screens_count = session.exec(
        select(func.count(Screen.id)).where(Screen.project_id == project.id)
    ).one()

    last_stats = None
    for r in runs:
        if r.stats_json:
            last_stats = json.loads(r.stats_json)
            break

    run_list = []
    for r in runs:
        stats = json.loads(r.stats_json) if r.stats_json else None
        run_list.append(
            {
                "id": r.id,
                "status": r.status,
                "current_phase": r.current_phase,
                "created_at": r.created_at.isoformat(),
                "error": r.error,
                "stats": stats,
            }
        )

    return templates.TemplateResponse(
        request,
        "project.html",
        context={
            "active_project": slug,
            "nav_projects": _nav_projects(session),
            "project": project,
            "runs": run_list,
            "screens_count": screens_count,
            "last_stats": last_stats,
        },
    )


@router.get("/projects/{slug}/screens", response_class=HTMLResponse)
def screens_gallery(
    request: Request,
    slug: str,
    status: str | None = None,
    session: Session = Depends(get_session),
):
    project = session.exec(select(Project).where(Project.slug == slug)).first()
    if not project:
        return RedirectResponse("/")

    query = select(Screen).where(Screen.project_id == project.id)
    if status:
        query = query.where(Screen.status == status)
    query = query.order_by(Screen.name)
    screens = session.exec(query).all()

    return templates.TemplateResponse(
        request,
        "screens.html",
        context={
            "active_project": slug,
            "nav_projects": _nav_projects(session),
            "project": project,
            "screens": screens,
            "filter_status": status,
        },
    )
