"""Web UI routes serving Jinja2 templates."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request
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


@router.post("/projects/{slug}/start-run")
def start_run(
    slug: str,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
):
    project = session.exec(select(Project).where(Project.slug == slug)).first()
    if not project:
        return RedirectResponse("/", status_code=303)

    run = Run(project_id=project.id, status="running")
    session.add(run)
    session.commit()
    session.refresh(run)

    background_tasks.add_task(_run_pipeline_bg, project.id, run.id)
    return RedirectResponse(f"/projects/{slug}/runs/{run.id}", status_code=303)


def _run_pipeline_bg(project_id: int, run_id: int) -> None:
    """Execute the full pipeline in background with proper config."""
    import json
    import os
    from datetime import datetime, timezone

    from figma_audit.db.engine import get_engine
    from figma_audit.utils.progress import RunProgress, set_progress

    engine = get_engine()
    with Session(engine) as session:
        run = session.get(Run, run_id)
        project = session.get(Project, project_id)
        if not run or not project:
            return

        run.started_at = datetime.now(timezone.utc)
        session.add(run)
        session.commit()

        progress = RunProgress()
        set_progress(progress)

        try:
            from figma_audit.config import Config

            cfg = Config(
                project=project.project_path,
                figma_url=project.figma_url,
                app_url=project.app_url,
                output=project.output_dir,
                figma_token=os.environ.get("FIGMA_TOKEN"),
                anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY"),
            )

            phases = ["analyze", "figma", "match", "capture", "compare", "report"]

            for phase_name in phases:
                run.current_phase = phase_name
                session.add(run)
                session.commit()
                progress.start_phase(phase_name)

                if phase_name == "analyze":
                    from figma_audit.phases.analyze_code import run as run_analyze
                    run_analyze(cfg)
                    progress.finish_phase()

                elif phase_name == "figma":
                    from figma_audit.phases.export_figma import run as run_figma
                    run_figma(cfg, offline=True)
                    progress.finish_phase()

                elif phase_name == "match":
                    from figma_audit.phases.match_screens import run as run_match
                    import yaml
                    mapping_path = run_match(cfg)
                    with open(mapping_path) as f:
                        data = yaml.safe_load(f)
                    if not data.get("verified"):
                        data["verified"] = True
                        with open(mapping_path, "w") as f:
                            yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
                    progress.finish_phase()

                elif phase_name == "capture":
                    from figma_audit.phases.capture_app import run as run_capture
                    run_capture(cfg)
                    progress.finish_phase()

                elif phase_name == "compare":
                    from figma_audit.phases.compare import run as run_compare
                    run_compare(cfg)
                    progress.finish_phase()

                elif phase_name == "report":
                    from figma_audit.phases.report import run as run_report
                    run_report(cfg)
                    progress.finish_phase()

            # Import results into DB
            from figma_audit.api.routes.runs import _import_results
            _import_results(session, project, run)

            run.status = "completed"
            run.finished_at = datetime.now(timezone.utc)

        except Exception as e:
            run.status = "failed"
            run.error = str(e)[:1000]
            run.finished_at = datetime.now(timezone.utc)

        session.add(run)
        session.commit()
        set_progress(None)


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
