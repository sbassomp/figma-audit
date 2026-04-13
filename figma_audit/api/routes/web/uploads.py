"""Upload routes: ZIP screen import + .fig file import.

Both endpoints kick off background processing and return a polling fragment
that the dashboard refreshes via htmx.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, Request, UploadFile
from fastapi.responses import HTMLResponse
from sqlmodel import Session, select

from figma_audit.api.deps import get_session
from figma_audit.api.routes.web._state import _upload_progress
from figma_audit.db.models import Project

router = APIRouter(tags=["web"])


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
        return "<div>Project not found</div>"

    # Save uploaded file to temp
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = tmp.name

    # Init progress
    _upload_progress[slug] = {
        "steps": [
            {"label": "Extracting ZIP", "status": "running", "detail": ""},
            {"label": "Converting PDF to PNG", "status": "pending", "detail": ""},
            {"label": "Matching with manifest", "status": "pending", "detail": ""},
            {"label": "Syncing to DB", "status": "pending", "detail": ""},
        ],
        "progress_current": 0,
        "progress_total": 0,
        "done": False,
        "error": None,
    }

    background_tasks.add_task(_process_upload_bg, slug, tmp_path, project.id)

    # Return initial progress fragment
    tmpl_dir = Path(__file__).parent.parent.parent.parent / "web" / "templates"
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
                progress["error"] = "Project not found"
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
            progress["steps"][0]["detail"] = f"{len(list(extract_dir.iterdir()))} files"

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
                progress["steps"][3]["detail"] = f"{updated} updated"

            progress["steps"][3]["status"] = "done"
            shutil.rmtree(extract_dir, ignore_errors=True)

    except Exception as e:
        progress["error"] = str(e)[:200]
    finally:
        Path(tmp_path).unlink(missing_ok=True)
        progress["done"] = True


# ── .fig file upload ──────────────────────────────────────────────


@router.post("/projects/{slug}/upload-fig", response_class=HTMLResponse)
def upload_fig(
    request: Request,
    slug: str,
    file: UploadFile,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
):
    """Upload a .fig file. Parses design tree, builds manifest, syncs screens to DB."""
    import shutil
    import tempfile

    project = session.exec(select(Project).where(Project.slug == slug)).first()
    if not project:
        return "<div>Project not found</div>"

    with tempfile.NamedTemporaryFile(suffix=".fig", delete=False) as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = tmp.name

    progress_key = f"{slug}_fig"
    _upload_progress[progress_key] = {
        "steps": [
            {"label": "Parsing .fig file", "status": "running", "detail": ""},
            {"label": "Identifying screens", "status": "pending", "detail": ""},
            {"label": "Extracting design tokens", "status": "pending", "detail": ""},
            {"label": "Syncing to DB", "status": "pending", "detail": ""},
        ],
        "progress_current": 0,
        "progress_total": 0,
        "done": False,
        "error": None,
    }

    background_tasks.add_task(_process_fig_upload_bg, slug, tmp_path, project.id)

    tmpl_dir = Path(__file__).parent.parent.parent.parent / "web" / "templates"
    from jinja2 import Environment, FileSystemLoader

    env = Environment(loader=FileSystemLoader(str(tmpl_dir)))
    tmpl = env.get_template("upload_progress.html")
    return HTMLResponse(
        tmpl.render(
            slug=slug,
            polling=True,
            progress_key="fig",
            **_upload_progress[progress_key],
        )
    )


def _process_fig_upload_bg(slug: str, tmp_path: str, project_id: int) -> None:
    """Parse .fig file in background, build manifest, sync screens to DB."""
    import json

    from sqlmodel import Session, select

    from figma_audit.db.engine import get_engine
    from figma_audit.db.models import Project
    from figma_audit.db.models import Screen as DBScreen

    progress_key = f"{slug}_fig"
    progress = _upload_progress[progress_key]

    try:
        engine = get_engine()
        with Session(engine) as session:
            project = session.get(Project, project_id)
            if not project:
                progress["error"] = "Project not found"
                progress["done"] = True
                return

            output_dir = Path(project.output_dir).expanduser().resolve()
            output_dir.mkdir(parents=True, exist_ok=True)
            manifest_path = output_dir / "figma_manifest.json"
            cache_dir = output_dir / "figma_raw"
            cache_dir.mkdir(parents=True, exist_ok=True)

            # Step 1: Parse .fig file
            from figma_audit.utils.fig_parser import parse_fig_file

            file_data = parse_fig_file(tmp_path)
            file_name = file_data.get("name", "design")

            # Cache the parsed tree
            with open(cache_dir / "file.json", "w") as f:
                json.dump(file_data, f, indent=2, ensure_ascii=False)

            progress["steps"][0]["status"] = "done"
            progress["steps"][0]["detail"] = file_name

            # Step 2: Identify screens
            progress["steps"][1]["status"] = "running"
            from figma_audit.phases.export_figma import _identify_screens

            screens = _identify_screens(file_data)
            progress["steps"][1]["status"] = "done"
            progress["steps"][1]["detail"] = f"{len(screens)} screens"

            # Step 3: Extract elements and build manifest
            progress["steps"][2]["status"] = "running"
            progress["progress_total"] = len(screens)

            from figma_audit.models import FigmaManifest, FigmaScreen
            from figma_audit.phases.export_figma import (
                _extract_background_color,
                _extract_elements,
            )
            from figma_audit.utils.figma_client import save_cache

            screens_dir = output_dir / "figma_screens"
            screens_dir.mkdir(parents=True, exist_ok=True)

            figma_screens = []
            for i, s in enumerate(screens):
                node = s["node"]
                bg = _extract_background_color(node)
                elements = _extract_elements(node)

                # Check if image already exists from a previous import-screens
                image_path = f"figma_screens/{s['filename']}"
                if not (output_dir / image_path).exists():
                    image_path = None

                figma_screens.append(
                    FigmaScreen(
                        id=s["id"],
                        name=s["name"],
                        page=s["page"],
                        width=s["width"],
                        height=s["height"],
                        image_path=image_path,
                        background_color=bg,
                        elements=elements,
                    )
                )
                progress["progress_current"] = i + 1

            file_key = Path(tmp_path).stem
            manifest = FigmaManifest(
                file_key=file_key,
                file_name=file_name,
                screens=figma_screens,
            )
            save_cache(manifest.model_dump(), manifest_path)

            total_elements = sum(len(s.elements) for s in figma_screens)
            progress["steps"][2]["status"] = "done"
            progress["steps"][2]["detail"] = f"{total_elements} tokens"
            progress["progress_current"] = 0
            progress["progress_total"] = 0

            # Step 4: Sync to DB
            progress["steps"][3]["status"] = "running"
            created = 0
            updated = 0
            imported_node_ids: set[str] = set()

            for s in manifest.screens:
                imported_node_ids.add(s.id)
                existing = session.exec(
                    select(DBScreen).where(
                        DBScreen.project_id == project.id,
                        DBScreen.figma_node_id == s.id,
                    )
                ).first()
                meta = json.dumps(
                    {
                        "background_color": s.background_color,
                        "element_count": len(s.elements),
                    }
                )
                if existing:
                    existing.name = s.name
                    existing.page = s.page
                    existing.width = s.width
                    existing.height = s.height
                    existing.metadata_json = meta
                    if s.image_path and not existing.image_path:
                        existing.image_path = s.image_path
                    # Restore if was obsolete (screen is back in the .fig)
                    if existing.status == "obsolete":
                        existing.status = "current"
                    session.add(existing)
                    updated += 1
                else:
                    # Inherit image from existing screen with same name
                    image_path = s.image_path
                    if not image_path:
                        sibling = session.exec(
                            select(DBScreen).where(
                                DBScreen.project_id == project.id,
                                DBScreen.name == s.name,
                                DBScreen.image_path.is_not(None),  # type: ignore[union-attr]
                            )
                        ).first()
                        if sibling:
                            image_path = sibling.image_path

                    screen = DBScreen(
                        project_id=project.id,
                        figma_node_id=s.id,
                        name=s.name,
                        page=s.page,
                        width=s.width,
                        height=s.height,
                        image_path=image_path,
                        metadata_json=meta,
                    )
                    session.add(screen)
                    created += 1

            # Mark screens absent from the new .fig as obsolete
            n_obsoleted = 0
            all_db_screens = session.exec(
                select(DBScreen).where(
                    DBScreen.project_id == project.id,
                    DBScreen.status == "current",
                )
            ).all()
            for sc in all_db_screens:
                if sc.figma_node_id not in imported_node_ids:
                    sc.status = "obsolete"
                    session.add(sc)
                    n_obsoleted += 1

            session.commit()

            # Invalidate cached mapping + manifest (force re-matching on next run)
            mapping_file = output_dir / "screen_mapping.yaml"
            manifest_file = output_dir / "pages_manifest.json"
            if mapping_file.exists():
                mapping_file.unlink()
            if manifest_file.exists():
                manifest_file.unlink()

            detail = f"{created} new, {updated} updated"
            if n_obsoleted:
                detail += f", {n_obsoleted} obsolete"
            progress["steps"][3]["status"] = "done"
            progress["steps"][3]["detail"] = detail

    except Exception as e:
        progress["error"] = str(e)[:200]
    finally:
        Path(tmp_path).unlink(missing_ok=True)
        progress["done"] = True
