"""Web UI routes serving Jinja2 templates."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, func, select

from figma_audit.api.deps import get_session
from figma_audit.db.models import Discrepancy, Project, Run, Screen

router = APIRouter(tags=["web"])

_templates_dir = Path(__file__).parent.parent.parent / "web" / "templates"
templates = Jinja2Templates(directory=str(_templates_dir))


def _nav_projects(session: Session) -> list[dict]:
    projects = session.exec(select(Project).order_by(Project.name)).all()
    return [{"name": p.name, "slug": p.slug} for p in projects]


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
            select(func.count(Discrepancy.id)).join(Run).where(
                Run.project_id == p.id,
                Discrepancy.severity == "critical",
                Discrepancy.status == "open",
            )
        ).one()

        fixed_count = session.exec(
            select(func.count(Discrepancy.id)).join(Run).where(
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

        project_list.append({
            "name": p.name,
            "slug": p.slug,
            "app_url": p.app_url,
            "last_run_date": last_run.created_at.strftime("%Y-%m-%d %H:%M") if last_run else None,
            "last_run_status": last_run.status if last_run else None,
        })

    return templates.TemplateResponse(request, "dashboard.html", context={
        "active_nav": "dashboard",
        "nav_projects": _nav_projects(session),
        "projects": project_list,
        "total_runs": total_runs,
        "total_critical": total_critical,
        "total_fixed": total_fixed,
    })


@router.get("/projects/new", response_class=HTMLResponse)
def new_project_form(request: Request, session: Session = Depends(get_session)):
    return templates.TemplateResponse(request, "new_project.html", context={
        "active_nav": "new_project",
        "nav_projects": _nav_projects(session),
    })


@router.post("/projects/new")
def create_project_form(
    name: str = Form(...),
    figma_url: str = Form(""),
    app_url: str = Form(""),
    project_path: str = Form(""),
    output_dir: str = Form("./output"),
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
        run_list.append({
            "id": r.id,
            "status": r.status,
            "current_phase": r.current_phase,
            "created_at": r.created_at.isoformat(),
            "error": r.error,
            "stats": stats,
        })

    return templates.TemplateResponse(request, "project.html", context={
        "active_project": slug,
        "nav_projects": _nav_projects(session),
        "project": project,
        "runs": run_list,
        "screens_count": screens_count,
        "last_stats": last_stats,
    })


@router.get("/projects/{slug}/screens", response_class=HTMLResponse)
def screens_gallery(
    request: Request, slug: str, status: str | None = None, session: Session = Depends(get_session)
):
    project = session.exec(select(Project).where(Project.slug == slug)).first()
    if not project:
        return RedirectResponse("/")

    query = select(Screen).where(Screen.project_id == project.id)
    if status:
        query = query.where(Screen.status == status)
    query = query.order_by(Screen.name)
    screens = session.exec(query).all()

    return templates.TemplateResponse(request, "screens.html", context={
        "active_project": slug,
        "nav_projects": _nav_projects(session),
        "project": project,
        "screens": screens,
        "filter_status": status,
    })


@router.get("/projects/{slug}/runs/{run_id}", response_class=HTMLResponse)
def run_detail(
    request: Request, slug: str, run_id: int,
    severity: str | None = None,
    session: Session = Depends(get_session),
):
    project = session.exec(select(Project).where(Project.slug == slug)).first()
    if not project:
        return RedirectResponse("/")

    run = session.exec(
        select(Run).where(Run.id == run_id, Run.project_id == project.id)
    ).first()
    if not run:
        return RedirectResponse(f"/projects/{slug}")

    query = select(Discrepancy).where(Discrepancy.run_id == run_id)
    if severity:
        query = query.where(Discrepancy.severity == severity)
    query = query.order_by(Discrepancy.severity, Discrepancy.category)
    discrepancies = session.exec(query).all()

    from figma_audit.db.models import Capture
    captures = session.exec(select(Capture).where(Capture.run_id == run_id)).all()

    by_severity = {"critical": 0, "important": 0, "minor": 0}
    by_category: dict[str, int] = {}
    all_discs = session.exec(select(Discrepancy).where(Discrepancy.run_id == run_id)).all()
    for d in all_discs:
        by_severity[d.severity] = by_severity.get(d.severity, 0) + 1
        by_category[d.category] = by_category.get(d.category, 0) + 1

    # Group discrepancies by page_id for comparison links
    pages_with_discs = {}
    for d in all_discs:
        if d.page_id not in pages_with_discs:
            pages_with_discs[d.page_id] = {"count": 0, "screen_id": d.screen_id, "fidelity": d.overall_fidelity}
        pages_with_discs[d.page_id]["count"] += 1

    return templates.TemplateResponse(request, "run.html", context={
        "active_project": slug,
        "nav_projects": _nav_projects(session),
        "project": project,
        "run": {
            "id": run.id,
            "status": run.status,
            "current_phase": run.current_phase,
            "created_at": run.created_at.isoformat(),
            "error": run.error,
        },
        "discrepancies": discrepancies,
        "filter_severity": severity,
        "pages_with_discs": pages_with_discs,
        "stats": {
            "total_discrepancies": len(all_discs),
            "total_captures": len(captures),
            "by_severity": by_severity,
            "by_category": by_category,
        },
    })


@router.get("/projects/{slug}/runs/{run_id}/compare/{page_id}", response_class=HTMLResponse)
def comparison_view(
    request: Request, slug: str, run_id: int, page_id: str,
    session: Session = Depends(get_session),
):
    project = session.exec(select(Project).where(Project.slug == slug)).first()
    if not project:
        return RedirectResponse("/")

    run = session.exec(
        select(Run).where(Run.id == run_id, Run.project_id == project.id)
    ).first()
    if not run:
        return RedirectResponse(f"/projects/{slug}")

    # Get discrepancies for this page
    discs = session.exec(
        select(Discrepancy).where(
            Discrepancy.run_id == run_id,
            Discrepancy.page_id == page_id,
        ).order_by(Discrepancy.severity)
    ).all()

    fidelity = discs[0].overall_fidelity if discs else "unknown"

    # Get the screen
    screen_id = discs[0].screen_id if discs else None
    screen = session.get(Screen, screen_id) if screen_id else None

    # Get the capture
    from figma_audit.db.models import Capture
    capture = session.exec(
        select(Capture).where(Capture.run_id == run_id, Capture.page_id == page_id)
    ).first()

    if not screen:
        screen = type("FakeScreen", (), {"name": page_id, "image_path": None})()
    if not capture:
        capture = type("FakeCapture", (), {"page_id": page_id, "route": "", "screenshot_path": None})()

    return templates.TemplateResponse(request, "comparison.html", context={
        "active_project": slug,
        "nav_projects": _nav_projects(session),
        "project": project,
        "run_id": run_id,
        "screen": screen,
        "capture": capture,
        "discrepancies": discs,
        "fidelity": fidelity,
    })
