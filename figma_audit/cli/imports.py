"""``import-screens`` command: import Figma screen images from a ZIP / directory."""

from __future__ import annotations

from pathlib import Path

import click

from figma_audit.cli.group import _find_config, cli, console
from figma_audit.config import Config


@cli.command(name="import-screens")
@click.argument("source", type=click.Path(exists=True))
@click.option("--output", "-o", default=None, help="Output directory")
@click.option("--config", "config_path", type=click.Path(exists=True), help="Config YAML file")
def import_screens(source: str, output: str | None, config_path: str | None) -> None:
    """Import Figma screen images from a zip file or directory (exported from Figma Desktop)."""
    import json
    import re
    import shutil
    import subprocess
    import tempfile
    import zipfile

    cfg = Config.load(config_path=_find_config(config_path), output=output)
    screens_dir = cfg.figma_screens_dir
    screens_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = cfg.output_dir / "figma_manifest.json"

    if not manifest_path.exists():
        console.print("[red]figma_manifest.json not found. Run Phase 2 first.[/red]")
        return

    source_path = Path(source)

    # Extract zip if needed
    if source_path.suffix == ".zip":
        extract_dir = Path(tempfile.mkdtemp())
        console.print(f"Extracting {source_path.name}...")
        with zipfile.ZipFile(source_path) as zf:
            zf.extractall(extract_dir)
        source_dir = extract_dir
    else:
        source_dir = source_path
        extract_dir = None

    # Convert PDFs to PNGs
    pdf_files = list(source_dir.glob("*.pdf"))
    png_files = list(source_dir.glob("*.png"))
    console.print(f"Found {len(pdf_files)} PDFs, {len(png_files)} PNGs")

    def slugify(name: str) -> str:
        s = re.sub(r"[^\\w\\s-]", "", name.lower().strip())
        s = re.sub(r"[\\s_]+", "-", s)
        return re.sub(r"-+", "-", s).strip("-")

    converted = 0
    for pdf in pdf_files:
        slug = slugify(pdf.stem)
        dest = screens_dir / f"{slug}.png"
        if dest.exists() and dest.stat().st_size > 0:
            converted += 1
            continue
        try:
            subprocess.run(
                [
                    "pdftoppm",
                    "-png",
                    "-r",
                    "150",
                    "-singlefile",
                    str(pdf),
                    str(dest.with_suffix("")),
                ],
                capture_output=True,
                timeout=10,
                check=True,
            )
            converted += 1
        except Exception as e:
            console.print(f"  [dim]Convert failed {pdf.name}: {e}[/dim]")

    # Copy PNGs directly
    for png in png_files:
        slug = slugify(png.stem)
        dest = screens_dir / f"{slug}.png"
        if not dest.exists():
            shutil.copy2(png, dest)
            converted += 1

    console.print(f"  {converted} images in {screens_dir}")

    # Match to manifest
    with open(manifest_path) as f:
        manifest = json.load(f)

    available = {p.stem: p.name for p in screens_dir.glob("*.png")}
    matched = 0
    for screen in manifest["screens"]:
        if screen.get("image_path") and (cfg.output_dir / screen["image_path"]).exists():
            matched += 1
            continue
        slug = slugify(screen["name"])
        if slug in available:
            screen["image_path"] = f"figma_screens/{available[slug]}"
            matched += 1
        else:
            for png_slug, png_name in available.items():
                if slug.replace("-", "") == png_slug.replace("-", ""):
                    screen["image_path"] = f"figma_screens/{png_name}"
                    matched += 1
                    break

    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)

    total_screens = len(manifest["screens"])
    console.print(f"[bold green]{matched}/{total_screens} screens with images[/bold green]")

    # Sync image_path to DB if it exists
    db_path = Path("figma-audit.db")
    if db_path.exists():
        try:
            from sqlmodel import Session, select

            from figma_audit.db.engine import get_engine, init_db
            from figma_audit.db.models import Screen as DBScreen

            init_db(str(db_path))
            engine = get_engine(str(db_path))
            manifest_images = {
                s["id"]: s["image_path"] for s in manifest["screens"] if s.get("image_path")
            }
            updated = 0
            with Session(engine) as session:
                for sc in session.exec(select(DBScreen)).all():
                    new_path = manifest_images.get(sc.figma_node_id)
                    if new_path and sc.image_path != new_path:
                        sc.image_path = new_path
                        session.add(sc)
                        updated += 1
                session.commit()
            if updated:
                console.print(f"  DB synced: {updated} screen image paths updated")
        except Exception as e:
            console.print(f"  [dim]DB sync skipped: {e}[/dim]")

    if extract_dir:
        shutil.rmtree(extract_dir, ignore_errors=True)
