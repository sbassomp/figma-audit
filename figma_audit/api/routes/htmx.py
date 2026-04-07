"""htmx-specific endpoints returning HTML fragments."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlmodel import Session, select

from figma_audit.api.deps import get_project, get_session
from figma_audit.db.models import Discrepancy, Project, Screen

router = APIRouter(prefix="/htmx/projects/{slug}", tags=["htmx"])


def _disc_card_html(d: Discrepancy, slug: str, run_id: int | None = None) -> str:
    """Render a single discrepancy card as HTML fragment."""
    values_html = ""
    if d.figma_value or d.app_value:
        parts = []
        if d.figma_value:
            parts.append(f"<span>Figma: <code>{d.figma_value}</code></span>")
        if d.app_value:
            parts.append(f"<span>App: <code>{d.app_value}</code></span>")
        values_html = f'<div class="discrepancy-values">{"".join(parts)}</div>'

    actions_html = ""
    if d.status == "open":
        base_url = f"/htmx/projects/{slug}/discrepancies/{d.id}/status"
        hx_tgt = f'hx-target="#disc-{d.id}" hx-swap="outerHTML"'
        actions_html = (
            f'<div class="discrepancy-actions">'
            f'<button class="btn btn-sm" '
            f'hx-post="{base_url}/ignored" {hx_tgt}>Ignorer</button>'
            f'<button class="btn btn-sm" '
            f'hx-post="{base_url}/wontfix" {hx_tgt}>Won\'t fix</button>'
            f'<button class="btn btn-sm" '
            f'hx-post="{base_url}/fixed" {hx_tgt}>Corrige</button>'
            f"</div>"
        )

    compare_url = f"/projects/{slug}/runs/{run_id}/compare/{d.page_id}"
    link_style = "color: var(--text-muted); text-decoration: none;"
    link_text = f"{d.category} - {d.page_id} ({d.route})"
    return f"""<div class="discrepancy {d.severity}" id="disc-{d.id}">
    <div class="discrepancy-header">
      <a href="{compare_url}" class="text-xs mono" style="{link_style}"
         title="Voir la comparaison">{link_text}</a>
      <div class="flex gap-1 items-center">
        <span class="badge badge-{d.status}">{d.status}</span>
        <span class="badge badge-{d.severity}">{d.severity}</span>
      </div>
    </div>
    <div class="discrepancy-desc">{d.description}</div>
    {values_html}
    {actions_html}
  </div>"""


def _screen_card_html(s: Screen, slug: str) -> str:
    """Render a single screen card as HTML fragment."""
    if s.image_path:
        img = f'<img src="/files/{slug}/{s.image_path}" alt="{s.name}" loading="lazy">'
    else:
        img = (
            '<div style="height:220px;background:var(--surface2);'
            "display:flex;align-items:center;"
            'justify-content:center;">'
            '<span class="text-muted text-sm">'
            "Pas d'image</span></div>"
        )

    mapped = f'<span class="mono">{s.mapped_route}</span>' if s.mapped_route else ""

    scr_base = f"/htmx/projects/{slug}/screens/{s.id}/status"
    scr_tgt = f'hx-target="#screen-{s.id}" hx-swap="outerHTML"'
    if s.status == "current":
        btn = (
            f'<button class="btn btn-sm" '
            f'hx-post="{scr_base}/obsolete" {scr_tgt} '
            f'hx-confirm="Marquer cet ecran comme obsolete ?">'
            f"Obsolete</button>"
        )
    elif s.status == "obsolete":
        btn = (
            f'<button class="btn btn-sm" hx-post="{scr_base}/current" {scr_tgt}>Restaurer</button>'
        )
    else:
        btn = ""

    return f"""<div class="screen-card" id="screen-{s.id}">
    {img}
    <div class="screen-info">
      <div class="screen-name" title="{s.name}">{s.name}</div>
      <div class="screen-meta">{s.width:.0f}x{s.height:.0f} {mapped}</div>
      <div class="flex justify-between items-center mt-1">
        <span class="badge badge-{s.status}">{s.status}</span>
        {btn}
      </div>
    </div>
  </div>"""


@router.get("/runs/{run_id}/progress", response_class=HTMLResponse)
def run_progress(
    slug: str,
    run_id: int,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> str:
    """Return progress HTML fragment, polled by htmx every 3s."""
    import json
    from pathlib import Path

    from figma_audit.db.models import Run
    from figma_audit.utils.progress import PHASE_LABELS

    run = session.exec(select(Run).where(Run.id == run_id, Run.project_id == project.id)).first()
    if not run:
        return "<div>Run not found</div>"

    tmpl_dir = Path(__file__).parent.parent.parent / "web" / "templates"
    from jinja2 import Environment, FileSystemLoader

    env = Environment(loader=FileSystemLoader(str(tmpl_dir)))
    tmpl = env.get_template("run_progress.html")

    # Read progress from DB
    if run.progress_json and run.status == "running":
        data = json.loads(run.progress_json)
        return tmpl.render(
            slug=slug,
            run_id=run_id,
            phases=data.get("phases", []),
            current_step=data.get("current_step", ""),
            current_progress=data.get("current_progress", 0),
            current_total=data.get("current_total", 0),
            elapsed=data.get("elapsed"),
            polling=True,
        )

    # Run is not active -- show final state (or progress from last save)
    if run.progress_json:
        data = json.loads(run.progress_json)
        return tmpl.render(
            slug=slug,
            run_id=run_id,
            phases=data.get("phases", []),
            current_step="",
            current_progress=0,
            current_total=0,
            elapsed=data.get("elapsed"),
            polling=False,
        )

    # No progress data at all -- show generic checklist
    all_phases = ["analyze", "figma", "match", "capture", "compare", "report"]
    phases = [
        {
            "name": name,
            "label": PHASE_LABELS.get(name, name),
            "status": "completed" if run.status == "completed" else "pending",
            "duration": None,
            "detail": None,
            "cost": None,
        }
        for name in all_phases
    ]
    return tmpl.render(
        slug=slug,
        run_id=run_id,
        phases=phases,
        current_step="",
        current_progress=0,
        current_total=0,
        elapsed=None,
        polling=False,
    )


@router.post("/discrepancies/{disc_id}/status/{new_status}", response_class=HTMLResponse)
def update_discrepancy_status(
    slug: str,
    disc_id: int,
    new_status: str,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> str:
    disc = session.get(Discrepancy, disc_id)
    if not disc:
        raise HTTPException(status_code=404)

    valid = ("open", "ignored", "acknowledged", "fixed", "wontfix")
    if new_status not in valid:
        raise HTTPException(status_code=400)

    disc.status = new_status
    session.add(disc)
    session.commit()
    session.refresh(disc)
    return _disc_card_html(disc, slug, run_id=disc.run_id)


@router.post("/screens/{screen_id}/status/{new_status}", response_class=HTMLResponse)
def update_screen_status(
    slug: str,
    screen_id: int,
    new_status: str,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> str:
    screen = session.exec(
        select(Screen).where(Screen.id == screen_id, Screen.project_id == project.id)
    ).first()
    if not screen:
        raise HTTPException(status_code=404)

    if new_status not in ("current", "obsolete", "draft", "component"):
        raise HTTPException(status_code=400)

    screen.status = new_status
    session.add(screen)
    session.commit()
    session.refresh(screen)
    return _screen_card_html(screen, slug)
