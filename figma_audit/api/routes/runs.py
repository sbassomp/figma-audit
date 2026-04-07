"""Run management routes."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from figma_audit.api.deps import get_project, get_session
from figma_audit.db.models import Capture, Discrepancy, Project, Run, Screen

router = APIRouter(prefix="/api/projects/{slug}/runs", tags=["runs"])


class RunCreate(BaseModel):
    from_phase: str | None = None


@router.get("")
def list_runs(
    slug: str,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> list[dict]:
    runs = session.exec(
        select(Run).where(Run.project_id == project.id).order_by(Run.created_at.desc())
    ).all()
    return [
        {
            "id": r.id,
            "status": r.status,
            "current_phase": r.current_phase,
            "from_phase": r.from_phase,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "finished_at": r.finished_at.isoformat() if r.finished_at else None,
            "stats": json.loads(r.stats_json) if r.stats_json else None,
            "error": r.error,
            "created_at": r.created_at.isoformat(),
        }
        for r in runs
    ]


@router.post("", status_code=201)
def create_run(
    slug: str,
    data: RunCreate,
    background_tasks: BackgroundTasks,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> dict:
    run = Run(
        project_id=project.id,
        status="pending",
        from_phase=data.from_phase,
    )
    session.add(run)
    session.commit()
    session.refresh(run)

    background_tasks.add_task(_execute_run, project.id, run.id, data.from_phase)

    return {"id": run.id, "status": run.status}


@router.get("/{run_id}")
def get_run(
    slug: str,
    run_id: int,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> dict:
    run = session.exec(select(Run).where(Run.id == run_id, Run.project_id == project.id)).first()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    discrepancies = session.exec(select(Discrepancy).where(Discrepancy.run_id == run.id)).all()

    captures = session.exec(select(Capture).where(Capture.run_id == run.id)).all()

    by_severity = {"critical": 0, "important": 0, "minor": 0}
    by_category: dict[str, int] = {}
    for d in discrepancies:
        by_severity[d.severity] = by_severity.get(d.severity, 0) + 1
        by_category[d.category] = by_category.get(d.category, 0) + 1

    return {
        "id": run.id,
        "status": run.status,
        "current_phase": run.current_phase,
        "from_phase": run.from_phase,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "error": run.error,
        "stats": {
            "total_discrepancies": len(discrepancies),
            "total_captures": len(captures),
            "by_severity": by_severity,
            "by_category": by_category,
        },
        "created_at": run.created_at.isoformat(),
    }


def _execute_run(project_id: int, run_id: int, from_phase: str | None) -> None:
    """Execute the audit pipeline in background. Updates run status in DB."""
    from figma_audit.db.engine import get_engine

    engine = get_engine()
    with Session(engine) as session:
        run = session.get(Run, run_id)
        project = session.get(Project, project_id)
        if not run or not project:
            return

        run.status = "running"
        run.started_at = datetime.now(timezone.utc)
        session.add(run)
        session.commit()

        try:
            from figma_audit.config import Config

            cfg = Config(
                project=project.project_path,
                figma_url=project.figma_url,
                app_url=project.app_url,
                output=project.output_dir,
            )

            phases = ["analyze", "figma", "match", "capture", "compare", "report"]
            if from_phase and from_phase in phases:
                phases = phases[phases.index(from_phase) :]

            for phase_name in phases:
                run.current_phase = phase_name
                session.add(run)
                session.commit()

                if phase_name == "analyze":
                    from figma_audit.phases.analyze_code import run as run_analyze

                    run_analyze(cfg)

                elif phase_name == "figma":
                    from figma_audit.phases.export_figma import run as run_figma

                    run_figma(cfg, offline=True)

                elif phase_name == "match":
                    import yaml

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
                    from figma_audit.phases.capture_app import run as run_capture

                    run_capture(cfg)

                elif phase_name == "compare":
                    from figma_audit.phases.compare import run as run_compare

                    run_compare(cfg)

                elif phase_name == "report":
                    from figma_audit.phases.report import run as run_report

                    run_report(cfg)

            # Import results into DB
            _import_results(session, project, run)

            run.status = "completed"
            run.finished_at = datetime.now(timezone.utc)

        except Exception as e:
            run.status = "failed"
            run.error = str(e)[:1000]
            run.finished_at = datetime.now(timezone.utc)

        session.add(run)
        session.commit()


def _import_results(session: Session, project: Project, run: Run) -> None:
    """Import phase output files into the database."""
    from pathlib import Path

    output_dir = Path(project.output_dir).expanduser().resolve()

    # Import screens from figma_manifest.json
    figma_manifest_path = output_dir / "figma_manifest.json"
    if figma_manifest_path.exists():
        with open(figma_manifest_path) as f:
            manifest = json.load(f)
        for s in manifest.get("screens", []):
            existing = session.exec(
                select(Screen).where(
                    Screen.project_id == project.id,
                    Screen.figma_node_id == s["id"],
                )
            ).first()
            if not existing:
                screen = Screen(
                    project_id=project.id,
                    figma_node_id=s["id"],
                    name=s["name"],
                    page=s.get("page", ""),
                    width=s.get("width", 0),
                    height=s.get("height", 0),
                    image_path=s.get("image_path"),
                    metadata_json=json.dumps(
                        {
                            "background_color": s.get("background_color"),
                            "element_count": len(s.get("elements", [])),
                        }
                    ),
                )
                session.add(screen)

    # Import mapping from screen_mapping.yaml
    import yaml

    mapping_path = output_dir / "screen_mapping.yaml"
    if mapping_path.exists():
        with open(mapping_path) as f:
            mapping = yaml.safe_load(f)
        for m in mapping.get("mappings", []):
            screen = session.exec(
                select(Screen).where(
                    Screen.project_id == project.id,
                    Screen.figma_node_id == m.get("figma_screen_id"),
                )
            ).first()
            if screen:
                screen.mapped_route = m.get("route")
                screen.mapped_page_id = m.get("page_id")
                screen.mapping_confidence = m.get("confidence")
                session.add(screen)

    # Import captures
    captures_path = output_dir / "app_captures.json"
    if captures_path.exists():
        with open(captures_path) as f:
            captures = json.load(f)
        for c in captures:
            capture = Capture(
                run_id=run.id,
                page_id=c["page_id"],
                route=c["route"],
                screenshot_path=c.get("screenshot"),
                styles_available=c.get("styles_available", False),
                error=c.get("error"),
            )
            session.add(capture)

    # Import discrepancies
    discrepancies_path = output_dir / "discrepancies.json"
    if discrepancies_path.exists():
        with open(discrepancies_path) as f:
            disc_data = json.load(f)

        # Build screen lookup
        screens = session.exec(select(Screen).where(Screen.project_id == project.id)).all()
        screen_by_page_id: dict[str, Screen] = {}
        for s in screens:
            if s.mapped_page_id:
                screen_by_page_id[s.mapped_page_id] = s

        for comp in disc_data.get("comparisons", []):
            screen = screen_by_page_id.get(comp.get("page_id"))
            for d in comp.get("discrepancies", []):
                disc = Discrepancy(
                    run_id=run.id,
                    screen_id=screen.id if screen else None,
                    page_id=comp["page_id"],
                    route=comp["route"],
                    category=d["category"],
                    severity=d["severity"],
                    description=d["description"],
                    figma_value=d.get("figma_value"),
                    app_value=d.get("app_value"),
                    location=d.get("location"),
                    overall_fidelity=comp.get("overall_fidelity"),
                )
                session.add(disc)

        # Store stats on run
        run.stats_json = json.dumps(disc_data.get("statistics", {}))

    session.commit()
