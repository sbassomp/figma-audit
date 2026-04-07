"""Web UI routes serving Jinja2 templates."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, func, select

from figma_audit import get_build_info
from figma_audit.api.deps import get_session
from figma_audit.db.models import Discrepancy, Project, Run, Screen

router = APIRouter(tags=["web"])

_templates_dir = Path(__file__).parent.parent.parent / "web" / "templates"
templates = Jinja2Templates(directory=str(_templates_dir))

# Inject build version into all templates
templates.env.globals["build_version"] = get_build_info()


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

        run_count = session.exec(select(func.count(Run.id)).where(Run.project_id == p.id)).one()

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


# Global upload progress state (per project slug)
_upload_progress: dict[str, dict] = {}


@router.post("/projects/{slug}/upload-screens", response_class=HTMLResponse)
def upload_screens(
    request: Request,
    slug: str,
    file: UploadFile,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
):
    """Upload a Figma export ZIP. Returns progress fragment, processes in background."""
    import shutil
    import tempfile

    project = session.exec(select(Project).where(Project.slug == slug)).first()
    if not project:
        return "<div>Projet non trouve</div>"

    # Save uploaded file to temp
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = tmp.name

    # Init progress
    _upload_progress[slug] = {
        "steps": [
            {"label": "Extraction du ZIP", "status": "running", "detail": ""},
            {"label": "Conversion PDF → PNG", "status": "pending", "detail": ""},
            {"label": "Matching avec le manifest", "status": "pending", "detail": ""},
            {"label": "Synchronisation DB", "status": "pending", "detail": ""},
        ],
        "progress_current": 0,
        "progress_total": 0,
        "done": False,
        "error": None,
    }

    background_tasks.add_task(_process_upload_bg, slug, tmp_path, project.id)

    # Return initial progress fragment
    tmpl_dir = Path(__file__).parent.parent.parent / "web" / "templates"
    from jinja2 import Environment, FileSystemLoader

    env = Environment(loader=FileSystemLoader(str(tmpl_dir)))
    tmpl = env.get_template("upload_progress.html")
    return HTMLResponse(
        tmpl.render(
            slug=slug,
            polling=True,
            **_upload_progress[slug],
        )
    )


def _process_upload_bg(slug: str, tmp_path: str, project_id: int) -> None:
    """Process ZIP upload in background, updating progress state."""
    import json
    import re
    import shutil
    import subprocess
    import zipfile

    from sqlmodel import Session, select

    from figma_audit.db.engine import get_engine
    from figma_audit.db.models import Project
    from figma_audit.db.models import Screen as DBScreen

    progress = _upload_progress[slug]

    try:
        engine = get_engine()
        with Session(engine) as session:
            project = session.get(Project, project_id)
            if not project:
                progress["error"] = "Projet non trouve"
                progress["done"] = True
                return

            output_dir = Path(project.output_dir).expanduser().resolve()
            screens_dir = output_dir / "figma_screens"
            screens_dir.mkdir(parents=True, exist_ok=True)
            manifest_path = output_dir / "figma_manifest.json"

            def slugify(name: str) -> str:
                s = re.sub(r"[^\w\s-]", "", name.lower().strip())
                s = re.sub(r"[\s_]+", "-", s)
                return re.sub(r"-+", "-", s).strip("-")

            # Step 1: Extract ZIP
            import tempfile

            extract_dir = Path(tempfile.mkdtemp())
            with zipfile.ZipFile(tmp_path) as zf:
                zf.extractall(extract_dir)
            progress["steps"][0]["status"] = "done"
            progress["steps"][0]["detail"] = f"{len(list(extract_dir.iterdir()))} fichiers"

            # Step 2: Convert PDFs to PNGs
            progress["steps"][1]["status"] = "running"
            pdfs = list(extract_dir.glob("*.pdf"))
            pngs_src = list(extract_dir.glob("*.png"))
            progress["progress_total"] = len(pdfs) + len(pngs_src)
            progress["progress_current"] = 0

            converted = 0
            for pdf in pdfs:
                slug_name = slugify(pdf.stem)
                dest = screens_dir / f"{slug_name}.png"
                if dest.exists() and dest.stat().st_size > 0:
                    converted += 1
                else:
                    try:
                        dest_stem = str(dest.with_suffix(""))
                        subprocess.run(
                            ["pdftoppm", "-png", "-r", "150", "-singlefile", str(pdf), dest_stem],
                            capture_output=True,
                            timeout=10,
                            check=True,
                        )
                        converted += 1
                    except Exception:
                        pass
                progress["progress_current"] += 1

            for png in pngs_src:
                slug_name = slugify(png.stem)
                dest = screens_dir / f"{slug_name}.png"
                if not dest.exists():
                    shutil.copy2(png, dest)
                    converted += 1
                progress["progress_current"] += 1

            progress["steps"][1]["status"] = "done"
            progress["steps"][1]["detail"] = f"{converted} images"
            progress["progress_current"] = 0
            progress["progress_total"] = 0

            # Step 3: Match to manifest
            progress["steps"][2]["status"] = "running"
            matched = 0
            if manifest_path.exists():
                with open(manifest_path) as f:
                    manifest = json.load(f)
                available = {p.stem: p.name for p in screens_dir.glob("*.png")}
                for screen in manifest["screens"]:
                    if screen.get("image_path") and (output_dir / screen["image_path"]).exists():
                        matched += 1
                        continue
                    s = slugify(screen["name"])
                    if s in available:
                        screen["image_path"] = f"figma_screens/{available[s]}"
                        matched += 1
                    else:
                        for png_slug, png_name in available.items():
                            if s.replace("-", "") == png_slug.replace("-", ""):
                                screen["image_path"] = f"figma_screens/{png_name}"
                                matched += 1
                                break
                with open(manifest_path, "w") as f:
                    json.dump(manifest, f, indent=2, ensure_ascii=False)
            progress["steps"][2]["status"] = "done"
            progress["steps"][2]["detail"] = f"{matched} matches"

            # Step 4: Sync to DB
            progress["steps"][3]["status"] = "running"
            if manifest_path.exists():
                with open(manifest_path) as f:
                    manifest = json.load(f)
                manifest_images = {
                    s["id"]: s["image_path"] for s in manifest["screens"] if s.get("image_path")
                }
                updated = 0
                db_screens = session.exec(
                    select(DBScreen).where(DBScreen.project_id == project.id)
                ).all()
                for sc in db_screens:
                    new_path = manifest_images.get(sc.figma_node_id)
                    if new_path and sc.image_path != new_path:
                        sc.image_path = new_path
                        session.add(sc)
                        updated += 1
                session.commit()
                progress["steps"][3]["detail"] = f"{updated} maj"

            progress["steps"][3]["status"] = "done"
            shutil.rmtree(extract_dir, ignore_errors=True)

    except Exception as e:
        progress["error"] = str(e)[:200]
    finally:
        Path(tmp_path).unlink(missing_ok=True)
        progress["done"] = True


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

            # Try loading project config YAML if it exists
            config_candidates = [
                Path(project.output_dir).parent / "figma-audit.yaml",
                Path(project.output_dir) / "figma-audit.yaml",
                Path.home() / "dev" / "figma-audit" / "figma-audit.yaml",
            ]
            config_path = None
            for cp in config_candidates:
                if cp.exists():
                    config_path = cp
                    break

            cfg = Config.load(
                config_path=config_path,
                project=project.project_path,
                figma_url=project.figma_url,
                app_url=project.app_url,
                output=project.output_dir,
            )

            # Override credentials from project DB (set via web form)
            if project.test_email:
                cfg.test_credentials.email = project.test_email
                cfg.test_credentials.otp = project.test_otp
            if project.seed_email:
                cfg.seed_account.email = project.seed_email
                cfg.seed_account.otp = project.seed_otp

            phases = ["analyze", "figma", "match", "capture", "compare", "report"]

            def _save_progress():
                run.current_phase = progress.current_phase
                run.progress_json = json.dumps(progress.to_dict())
                session.add(run)
                session.commit()

            def _step(msg: str) -> None:
                progress.update(step=msg)
                _save_progress()

            for phase_name in phases:
                progress.start_phase(phase_name)
                _save_progress()

                if phase_name == "analyze":
                    _step("Lecture des fichiers source (~133K tokens)...")
                    from figma_audit.phases.analyze_code import run as run_analyze

                    run_analyze(cfg)

                elif phase_name == "figma":
                    _step("Lecture du cache Figma...")
                    from figma_audit.phases.export_figma import run as run_figma

                    run_figma(cfg, offline=True)

                elif phase_name == "match":
                    import yaml

                    _step("Envoi des ecrans a Claude Vision...")
                    from figma_audit.phases.match_screens import run as run_match

                    mapping_path = run_match(cfg)
                    with open(mapping_path) as f:
                        data = yaml.safe_load(f)
                    if not data.get("verified"):
                        data["verified"] = True
                        with open(mapping_path, "w") as f:
                            yaml.dump(
                                data,
                                f,
                                default_flow_style=False,
                                allow_unicode=True,
                                sort_keys=False,
                            )

                elif phase_name == "capture":
                    _step("Login + creation donnees de test...")
                    from figma_audit.phases.capture_app import run as run_capture

                    run_capture(cfg)

                elif phase_name == "compare":
                    _step("Comparaison Figma vs App par Claude Vision...")
                    from figma_audit.phases.compare import run as run_compare

                    run_compare(cfg)

                elif phase_name == "report":
                    _step("Generation du rapport HTML...")
                    from figma_audit.phases.report import run as run_report

                    run_report(cfg)

                progress.finish_phase()
                _save_progress()

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


@router.get("/projects/{slug}/runs/{run_id}", response_class=HTMLResponse)
def run_detail(
    request: Request,
    slug: str,
    run_id: int,
    severity: str | None = None,
    status: str | None = None,
    session: Session = Depends(get_session),
):
    project = session.exec(select(Project).where(Project.slug == slug)).first()
    if not project:
        return RedirectResponse("/")

    run = session.exec(select(Run).where(Run.id == run_id, Run.project_id == project.id)).first()
    if not run:
        return RedirectResponse(f"/projects/{slug}")

    query = select(Discrepancy).where(Discrepancy.run_id == run_id)
    if severity:
        query = query.where(Discrepancy.severity == severity)
    if status:
        query = query.where(Discrepancy.status == status)
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
            pages_with_discs[d.page_id] = {
                "count": 0,
                "screen_id": d.screen_id,
                "fidelity": d.overall_fidelity,
            }
        pages_with_discs[d.page_id]["count"] += 1

    return templates.TemplateResponse(
        request,
        "run.html",
        context={
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
            "filter_status": status,
            "pages_with_discs": pages_with_discs,
            "stats": {
                "total_discrepancies": len(all_discs),
                "total_captures": len(captures),
                "by_severity": by_severity,
                "by_category": by_category,
            },
        },
    )


@router.get("/projects/{slug}/runs/{run_id}/compare/{page_id}", response_class=HTMLResponse)
def comparison_view(
    request: Request,
    slug: str,
    run_id: int,
    page_id: str,
    session: Session = Depends(get_session),
):
    project = session.exec(select(Project).where(Project.slug == slug)).first()
    if not project:
        return RedirectResponse("/")

    run = session.exec(select(Run).where(Run.id == run_id, Run.project_id == project.id)).first()
    if not run:
        return RedirectResponse(f"/projects/{slug}")

    # Get discrepancies for this page
    discs = session.exec(
        select(Discrepancy)
        .where(
            Discrepancy.run_id == run_id,
            Discrepancy.page_id == page_id,
        )
        .order_by(Discrepancy.severity)
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
        capture = type(
            "FakeCapture",
            (),
            {"page_id": page_id, "route": "", "screenshot_path": None},
        )()

    return templates.TemplateResponse(
        request,
        "comparison.html",
        context={
            "active_project": slug,
            "nav_projects": _nav_projects(session),
            "project": project,
            "run_id": run_id,
            "screen": screen,
            "capture": capture,
            "discrepancies": discs,
            "fidelity": fidelity,
        },
    )
