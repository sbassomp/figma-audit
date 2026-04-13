"""Run lifecycle pages: start a run, run detail, comparison view.

The actual pipeline executor (:func:`_run_pipeline_bg`) lives here too
because it is the BackgroundTask handed to FastAPI from ``start_run``.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session, select

from figma_audit.api.deps import get_session
from figma_audit.api.routes.web._state import _nav_projects, templates
from figma_audit.db.models import Discrepancy, Project, Run, Screen

router = APIRouter(tags=["web"])


@router.post("/projects/{slug}/start-run")
def start_run(
    slug: str,
    background_tasks: BackgroundTasks,
    agentic: str | None = Form(default=None),
    analyze_model: str | None = Form(default=None),
    session: Session = Depends(get_session),
):
    project = session.exec(select(Project).where(Project.slug == slug)).first()
    if not project:
        return RedirectResponse("/", status_code=303)

    run = Run(project_id=project.id, status="running")
    session.add(run)
    session.commit()
    session.refresh(run)

    background_tasks.add_task(
        _run_pipeline_bg,
        project.id,
        run.id,
        agentic=bool(agentic),
        analyze_model=analyze_model or None,
    )
    return RedirectResponse(f"/projects/{slug}/runs/{run.id}", status_code=303)


def _run_pipeline_bg(
    project_id: int,
    run_id: int,
    *,
    agentic: bool = False,
    analyze_model: str | None = None,
) -> None:
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

            if agentic:
                cfg.analyze_mode = "agentic"
            if analyze_model:
                cfg.analyze_model = analyze_model

            phases = ["analyze", "figma", "match", "capture", "compare", "report"]

            def _save_progress():
                run.current_phase = progress.current_phase
                run.progress_json = json.dumps(progress.to_dict())
                session.add(run)
                session.commit()

            def _step(msg: str) -> None:
                progress.update(step=msg)
                _save_progress()

            def _phase_cost(phase_name: str) -> tuple[float, int]:
                """Retrieve cost and tokens from the AI phase's client."""
                _phase_modules = {
                    "analyze": "figma_audit.phases.analyze_code",
                    "match": "figma_audit.phases.match_screens",
                    "compare": "figma_audit.phases.compare",
                }
                import sys

                mod_name = _phase_modules.get(phase_name, "")
                mod = sys.modules.get(mod_name)
                if mod:
                    client = getattr(mod, "_last_client", None)
                    if client:
                        return client.usage.cost(client.model), client.usage.total_tokens
                return 0.0, 0

            def _count_json(filename: str, key: str) -> int:
                path = Path(cfg.output_dir) / filename
                if path.exists():
                    with open(path) as f:
                        return len(json.load(f).get(key, []))
                return 0

            for phase_name in phases:
                progress.start_phase(phase_name)
                _save_progress()

                if phase_name == "analyze":
                    manifest_file = Path(cfg.output_dir) / "pages_manifest.json"
                    if manifest_file.exists():
                        n_pages = _count_json("pages_manifest.json", "pages")
                        _step(f"Existing manifest ({n_pages} pages) — skip")
                        progress.finish_phase(detail=f"{n_pages} pages (cached)")
                        _save_progress()
                        continue

                    _step("Reading source files...")
                    from figma_audit.phases.analyze_code import run as run_analyze

                    run_analyze(cfg)
                    cost, tokens = _phase_cost("analyze")
                    n_pages = _count_json("pages_manifest.json", "pages")
                    progress.finish_phase(detail=f"{n_pages} pages", cost=cost, tokens=tokens)

                elif phase_name == "figma":
                    _step("Reading Figma cache...")
                    from figma_audit.phases.export_figma import run as run_figma

                    run_figma(cfg, offline=True)
                    n_screens = _count_json("figma_manifest.json", "screens")
                    progress.finish_phase(detail=f"{n_screens} screens")

                elif phase_name == "match":
                    import yaml

                    mapping_path = Path(cfg.output_dir) / "screen_mapping.yaml"

                    # Skip if mapping already exists and is verified
                    if mapping_path.exists():
                        with open(mapping_path) as f:
                            existing = yaml.safe_load(f)
                        if existing and existing.get("verified"):
                            matched = sum(
                                1 for m in existing.get("mappings", []) if m.get("route")
                            )
                            _step(f"Existing mapping ({matched} matches) — skip")
                            progress.finish_phase(detail=f"{matched} matches (cached)")
                            _save_progress()
                            continue

                    _step("Sending screens to Claude Vision...")
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
                    matched = sum(1 for m in data.get("mappings", []) if m.get("route"))
                    cost, tokens = _phase_cost("match")
                    progress.finish_phase(
                        detail=f"{matched} matches", cost=cost, tokens=tokens
                    )

                elif phase_name == "capture":
                    _step("Login + creating test data...")
                    from figma_audit.phases.capture_app import run as run_capture

                    run_capture(cfg)
                    cap_path = Path(cfg.output_dir) / "app_captures.json"
                    n_caps = 0
                    if cap_path.exists():
                        with open(cap_path) as f:
                            cap_data = json.load(f)
                            n_caps = len(cap_data) if isinstance(cap_data, list) else 0
                    progress.finish_phase(detail=f"{n_caps} pages")

                elif phase_name == "compare":
                    _step("Comparing Figma vs App with Claude Vision...")
                    from figma_audit.phases.compare import run as run_compare

                    run_compare(cfg)
                    cost, tokens = _phase_cost("compare")
                    disc_path = Path(cfg.output_dir) / "discrepancies.json"
                    n_discs = 0
                    if disc_path.exists():
                        with open(disc_path) as f:
                            n_discs = (
                                json.load(f)
                                .get("statistics", {})
                                .get("total_discrepancies", 0)
                            )
                    progress.finish_phase(
                        detail=f"{n_discs} discrepancies", cost=cost, tokens=tokens
                    )

                elif phase_name == "report":
                    _step("Generating HTML report...")
                    from figma_audit.phases.report import run as run_report

                    report_path = run_report(cfg)
                    size_mb = report_path.stat().st_size / 1024 / 1024
                    progress.finish_phase(detail=f"{size_mb:.1f} MB")

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

    run = session.exec(
        select(Run).where(Run.id == run_id, Run.project_id == project.id)
    ).first()
    if not run:
        return RedirectResponse(f"/projects/{slug}")

    query = select(Discrepancy).where(Discrepancy.run_id == run_id)
    if severity:
        query = query.where(Discrepancy.severity == severity)
    if status == "ignored":
        # Show both ignored and wontfix together
        query = query.where(Discrepancy.status.in_(["ignored", "wontfix"]))  # type: ignore[union-attr]
    elif status:
        query = query.where(Discrepancy.status == status)
    else:
        # By default, hide dismissed (ignored/wontfix) — show only actionable
        query = query.where(Discrepancy.status.not_in(["ignored", "wontfix"]))  # type: ignore[union-attr]
    query = query.order_by(Discrepancy.severity, Discrepancy.category)
    discrepancies = session.exec(query).all()

    from figma_audit.db.models import Capture

    captures = session.exec(select(Capture).where(Capture.run_id == run_id)).all()

    # Capture success/failure breakdown — surfaces silent navigation failures
    # detected by Phase 4's global dedup pass.
    captures_ok = [c for c in captures if c.screenshot_path and not c.error]
    captures_failed = [c for c in captures if c.error]
    captures_dup = [c for c in captures_failed if c.error and "Duplicate" in c.error]

    by_severity = {"critical": 0, "important": 0, "minor": 0}
    by_category: dict[str, int] = {}
    all_discs = session.exec(select(Discrepancy).where(Discrepancy.run_id == run_id)).all()
    dismissed_statuses = {"ignored", "wontfix"}
    n_dismissed = 0
    n_mismatches = 0
    for d in all_discs:
        if d.status in dismissed_statuses:
            n_dismissed += 1
            continue
        if d.category == "MATCHING_ERROR":
            n_mismatches += 1
            continue
        by_severity[d.severity] = by_severity.get(d.severity, 0) + 1
        by_category[d.category] = by_category.get(d.category, 0) + 1

    # Group discrepancies by (page_id, screen_id) for comparison links
    # This separates multiple Figma screens matched to the same page
    comparisons_list = []
    seen_keys: set[tuple] = set()
    for d in all_discs:
        key = (d.page_id, d.screen_id)
        if key not in seen_keys:
            seen_keys.add(key)
            has_image = False
            screen_name = d.page_id
            if d.screen_id:
                sc = session.get(Screen, d.screen_id)
                has_image = bool(sc and sc.image_path)
                if sc:
                    screen_name = sc.name
            # For mismatches, extract the reason from the MATCHING_ERROR description
            mismatch_reason = ""
            if d.overall_fidelity == "mismatch" and d.category == "MATCHING_ERROR":
                mismatch_reason = d.description[:150]

            comparisons_list.append(
                {
                    "page_id": d.page_id,
                    "screen_id": d.screen_id,
                    "screen_name": screen_name,
                    "count": 0,
                    "fidelity": d.overall_fidelity,
                    "has_image": has_image,
                    "mismatch_reason": mismatch_reason,
                }
            )
        # Increment count for this key
        for comp in comparisons_list:
            if (comp["page_id"], comp["screen_id"]) == key:
                comp["count"] += 1
                break

    # Parse execution details from progress_json (available for completed/failed runs)
    execution = None
    if run.progress_json:
        try:
            execution = json.loads(run.progress_json)
        except (ValueError, TypeError):
            pass

    # Compute run duration
    run_duration = None
    if run.started_at and run.finished_at:
        run_duration = (run.finished_at - run.started_at).total_seconds()

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
                "started_at": run.started_at.isoformat() if run.started_at else None,
                "finished_at": run.finished_at.isoformat() if run.finished_at else None,
                "duration": run_duration,
                "error": run.error,
            },
            "execution": execution,
            "discrepancies": discrepancies,
            "filter_severity": severity,
            "filter_status": status,
            "comparisons_list": comparisons_list,
            "stats": {
                "total_discrepancies": len(all_discs) - n_dismissed,
                "n_dismissed": n_dismissed,
                "total_captures": len(captures),
                "captures_ok": len(captures_ok),
                "captures_failed": len(captures_failed),
                "captures_duplicate": len(captures_dup),
                "by_severity": by_severity,
                "by_category": by_category,
            },
            "failed_captures": [
                {
                    "page_id": c.page_id,
                    "route": c.route,
                    "landed_url": c.landed_url,
                    "error": c.error or "",
                    "is_duplicate": c in captures_dup,
                }
                for c in captures_failed
            ],
        },
    )


@router.get("/projects/{slug}/runs/{run_id}/compare/{page_id}", response_class=HTMLResponse)
def comparison_view(
    request: Request,
    slug: str,
    run_id: int,
    page_id: str,
    screen_id: int | None = None,
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

    # Get discrepancies for this page, optionally filtered by screen
    query = select(Discrepancy).where(
        Discrepancy.run_id == run_id,
        Discrepancy.page_id == page_id,
    )
    if screen_id:
        query = query.where(Discrepancy.screen_id == screen_id)
    discs = session.exec(query.order_by(Discrepancy.severity)).all()

    fidelity = discs[0].overall_fidelity if discs else "unknown"

    # Get the screen
    disc_screen_id = screen_id or (discs[0].screen_id if discs else None)
    screen = session.get(Screen, disc_screen_id) if disc_screen_id else None

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
            {"page_id": page_id, "route": "", "landed_url": None, "screenshot_path": None},
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
