"""CLI entry point for figma-audit."""

from __future__ import annotations

from pathlib import Path

import click
from rich.console import Console

from figma_audit.config import Config
from figma_audit.utils.checks import check_api_keys, check_playwright_browser, load_env_file

console = Console()

# Auto-discover config file in current directory
DEFAULT_CONFIG_NAMES = ["figma-audit.yaml", "figma-audit.yml"]


def _find_config(config_path: str | None) -> Path | None:
    if config_path:
        return Path(config_path)
    for name in DEFAULT_CONFIG_NAMES:
        p = Path(name)
        if p.exists():
            return p
    return None


@click.group()
@click.version_option(
    version=None,
    package_name="figma-audit",
    message="%(prog)s " + __import__("figma_audit").get_build_info(),
)
def cli() -> None:
    """figma-audit: Semantic comparison between Figma designs and deployed web apps."""


@cli.command()
@click.option("--figma-url", envvar="FIGMA_URL", help="Figma file URL")
@click.option("--figma-token", envvar="FIGMA_TOKEN", help="Figma API token")
@click.option("--output", "-o", default=None, help="Output directory")
@click.option("--config", "config_path", type=click.Path(exists=True), help="Config YAML file")
@click.option("--force-refresh", is_flag=True, help="Force re-download from Figma API")
@click.option("--offline", is_flag=True, help="Work from local cache only, no API calls")
@click.option("--target-page", help="Figma page ID to focus on (e.g. '45:927')")
def figma(
    figma_url: str | None,
    figma_token: str | None,
    output: str | None,
    config_path: str | None,
    force_refresh: bool,
    offline: bool,
    target_page: str | None,
) -> None:
    """Phase 2: Export Figma file -- download tree, extract tokens, export PNGs."""
    cfg = Config.load(
        config_path=_find_config(config_path),
        figma_url=figma_url,
        figma_token=figma_token,
        output=output,
    )

    from figma_audit.phases.export_figma import run

    manifest_path = run(cfg, force_refresh=force_refresh, offline=offline, target_page=target_page)
    console.print(f"\n[bold]Done. Manifest: {manifest_path}[/bold]")


@cli.command()
@click.option("--project", "-p", default=None, help="Path to the project to analyze")
@click.option("--output", "-o", default=None, help="Output directory")
@click.option("--config", "config_path", type=click.Path(exists=True), help="Config YAML file")
def analyze(
    project: str | None,
    output: str | None,
    config_path: str | None,
) -> None:
    """Phase 1: Analyze project code -- detect framework, extract routes, produce manifest."""
    load_env_file()
    cfg = Config.load(
        config_path=_find_config(config_path),
        project=project,
        output=output,
    )

    from figma_audit.phases.analyze_code import run

    manifest_path = run(cfg)
    console.print(f"\n[bold]Done. Manifest: {manifest_path}[/bold]")


@cli.command()
@click.option("--output", "-o", default=None, help="Output directory")
@click.option("--config", "config_path", type=click.Path(exists=True), help="Config YAML file")
def match(
    output: str | None,
    config_path: str | None,
) -> None:
    """Phase 3: Match Figma screens to application routes using AI Vision."""
    load_env_file()
    cfg = Config.load(
        config_path=_find_config(config_path),
        output=output,
    )

    from figma_audit.phases.match_screens import run

    mapping_path = run(cfg)
    console.print(f"\n[bold]Done. Mapping: {mapping_path}[/bold]")


@cli.command()
@click.option("--app-url", envvar="APP_URL", default=None, help="Deployed app URL")
@click.option("--output", "-o", default=None, help="Output directory")
@click.option("--config", "config_path", type=click.Path(exists=True), help="Config YAML file")
def capture(
    app_url: str | None,
    output: str | None,
    config_path: str | None,
) -> None:
    """Phase 4: Capture app screenshots via Playwright."""
    load_env_file()
    cfg = Config.load(
        config_path=_find_config(config_path),
        app_url=app_url,
        output=output,
    )

    from figma_audit.phases.capture_app import run

    captures_path = run(cfg)
    console.print(f"\n[bold]Done. Captures: {captures_path}[/bold]")


@cli.command()
@click.option("--output", "-o", default=None, help="Output directory")
@click.option("--config", "config_path", type=click.Path(exists=True), help="Config YAML file")
def compare(
    output: str | None,
    config_path: str | None,
) -> None:
    """Phase 5: Compare Figma designs against app screenshots."""
    load_env_file()
    cfg = Config.load(
        config_path=_find_config(config_path),
        output=output,
    )

    from figma_audit.phases.compare import run

    discrepancies_path = run(cfg)
    console.print(f"\n[bold]Done. Discrepancies: {discrepancies_path}[/bold]")


@cli.command()
@click.option("--output", "-o", default=None, help="Output directory")
@click.option("--config", "config_path", type=click.Path(exists=True), help="Config YAML file")
def report(
    output: str | None,
    config_path: str | None,
) -> None:
    """Phase 6: Generate standalone HTML report."""
    cfg = Config.load(
        config_path=_find_config(config_path),
        output=output,
    )

    from figma_audit.phases.report import run

    report_path = run(cfg)
    console.print(f"\n[bold]Done. Report: {report_path}[/bold]")


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


PHASE_ORDER = ["analyze", "figma", "match", "capture", "compare", "report"]
PHASE_NAMES = {
    "analyze": "Phase 1: Analyze code",
    "figma": "Phase 2: Export Figma",
    "match": "Phase 3: Match screens",
    "capture": "Phase 4: Capture app",
    "compare": "Phase 5: Compare",
    "report": "Phase 6: Report",
}


@cli.command()
@click.option("--config", "config_path", type=click.Path(exists=True), help="Config YAML file")
@click.option("--from", "from_phase", type=click.Choice(PHASE_ORDER), help="Resume from phase")
@click.option("--target-page", help="Figma page ID (Phase 2)")
@click.option("--offline", is_flag=True, help="Figma offline mode (Phase 2)")
def run(
    config_path: str | None,
    from_phase: str | None,
    target_page: str | None,
    offline: bool,
) -> None:
    """Run the full audit pipeline (all 6 phases)."""
    import sys

    from figma_audit.utils.progress import RunProgress, set_progress

    load_env_file()

    if not check_api_keys():
        sys.exit(1)

    cfg = Config.load(config_path=_find_config(config_path))

    phases = list(PHASE_ORDER)
    if from_phase:
        idx = phases.index(from_phase)
        phases = phases[idx:]

    progress = RunProgress(phases=phases)
    set_progress(progress)

    console.print(f"[bold]Running audit pipeline ({len(phases)} phases)[/bold]")

    for phase_name in phases:
        progress.start_phase(phase_name)

        if phase_name == "analyze":
            from figma_audit.phases.analyze_code import run as run_analyze

            run_analyze(cfg)
            client = _get_last_client("analyze_code")
            progress.finish_phase(
                detail=f"{_count_pages(cfg)} pages",
                cost=client.usage.cost(client.model) if client else 0,
                tokens=client.usage.total_tokens if client else 0,
            )

        elif phase_name == "figma":
            from figma_audit.phases.export_figma import run as run_figma

            run_figma(cfg, offline=offline, target_page=target_page)
            screens = _count_screens(cfg)
            progress.finish_phase(detail=f"{screens} ecrans")

        elif phase_name == "match":
            from figma_audit.phases.match_screens import run as run_match

            mapping_path = run_match(cfg)
            import yaml

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
            client = _get_last_client("match_screens")
            progress.finish_phase(
                detail=f"{matched} matches",
                cost=client.usage.cost(client.model) if client else 0,
                tokens=client.usage.total_tokens if client else 0,
            )

        elif phase_name == "capture":
            if not check_playwright_browser():
                sys.exit(1)
            from figma_audit.phases.capture_app import run as run_capture

            run_capture(cfg)
            captures = _count_captures(cfg)
            progress.finish_phase(detail=f"{captures} pages")

        elif phase_name == "compare":
            from figma_audit.phases.compare import run as run_compare

            run_compare(cfg)
            client = _get_last_client("compare")
            discs = _count_discrepancies(cfg)
            progress.finish_phase(
                detail=f"{discs} ecarts",
                cost=client.usage.cost(client.model) if client else 0,
                tokens=client.usage.total_tokens if client else 0,
            )

        elif phase_name == "report":
            from figma_audit.phases.report import run as run_report

            report_path = run_report(cfg)
            size_mb = report_path.stat().st_size / 1024 / 1024
            progress.finish_phase(detail=f"{size_mb:.1f} MB")

    progress.print_summary()
    set_progress(None)


def _get_last_client(module_hint: str):
    """Try to retrieve the ClaudeClient from a recently-run phase module."""
    # Phases create their client locally; we inspect the module globals
    # This is a best-effort approach
    import sys

    for mod_name, mod in sys.modules.items():
        if module_hint in mod_name and hasattr(mod, "client"):
            return mod.client
    return None


def _count_pages(cfg: Config) -> int:
    import json

    path = cfg.output_dir / "pages_manifest.json"
    if path.exists():
        with open(path) as f:
            return len(json.load(f).get("pages", []))
    return 0


def _count_screens(cfg: Config) -> int:
    import json

    path = cfg.output_dir / "figma_manifest.json"
    if path.exists():
        with open(path) as f:
            return len(json.load(f).get("screens", []))
    return 0


def _count_captures(cfg: Config) -> int:
    import json

    path = cfg.output_dir / "app_captures.json"
    if path.exists():
        with open(path) as f:
            return len(json.load(f))
    return 0


def _count_discrepancies(cfg: Config) -> int:
    import json

    path = cfg.output_dir / "discrepancies.json"
    if path.exists():
        with open(path) as f:
            return json.load(f).get("statistics", {}).get("total_discrepancies", 0)
    return 0


@cli.command()
@click.option("--host", default="127.0.0.1", help="Bind host")
@click.option("--port", default=8321, type=int, help="Bind port")
@click.option("--db", "db_path", default="figma-audit.db", help="SQLite database path")
def serve(host: str, port: int, db_path: str) -> None:
    """Start the figma-audit web server (API + dashboard)."""
    load_env_file()

    import uvicorn

    from figma_audit.api.app import create_app

    app = create_app(db_path=db_path)
    console.print(f"[bold]Starting figma-audit server on http://{host}:{port}[/bold]")
    console.print(f"  Database: {db_path}")
    console.print(f"  API docs: http://{host}:{port}/docs")
    uvicorn.run(app, host=host, port=port)


@cli.command()
def setup() -> None:
    """Interactive setup: configure API keys, install daemon, create DB."""
    import os
    import platform
    import subprocess
    import sys

    console.print("[bold]figma-audit setup[/bold]\n")

    config_dir = Path.home() / ".config" / "figma-audit"
    config_dir.mkdir(parents=True, exist_ok=True)
    env_file = config_dir / "env"
    db_path = config_dir / "figma-audit.db"

    # ── Step 1: API Keys ──────────────────────────────────────────
    console.print("[bold]1. Cles API[/bold]")
    existing_env = {}
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                existing_env[k.strip()] = v.strip()

    anthropic_key = existing_env.get("ANTHROPIC_API_KEY", os.environ.get("ANTHROPIC_API_KEY", ""))
    figma_token = existing_env.get("FIGMA_TOKEN", os.environ.get("FIGMA_TOKEN", ""))

    api_status = "[green]configure[/green]" if anthropic_key else "[red]manquante[/red]"
    figma_status = "[green]configure[/green]" if figma_token else "[red]manquante[/red]"
    console.print(f"  ANTHROPIC_API_KEY: {api_status}")
    console.print(f"  FIGMA_TOKEN:       {figma_status}")

    if not anthropic_key or click.confirm(
        "  Modifier ANTHROPIC_API_KEY ?", default=not anthropic_key
    ):
        anthropic_key = click.prompt("  ANTHROPIC_API_KEY", default=anthropic_key)

    if not figma_token or click.confirm("  Modifier FIGMA_TOKEN ?", default=not figma_token):
        figma_token = click.prompt("  FIGMA_TOKEN", default=figma_token)

    env_content = f"ANTHROPIC_API_KEY={anthropic_key}\nFIGMA_TOKEN={figma_token}\n"
    env_file.write_text(env_content)
    env_file.chmod(0o600)
    console.print(f"  [green]Cles sauvegardees dans {env_file}[/green]\n")

    # ── Step 2: Database ──────────────────────────────────────────
    console.print("[bold]2. Base de donnees[/bold]")
    from figma_audit.db.engine import init_db

    init_db(str(db_path))
    console.print(f"  [green]DB initialisee: {db_path}[/green]\n")

    # ── Step 3: Playwright ────────────────────────────────────────
    console.print("[bold]3. Navigateur (Playwright)[/bold]")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            capture_output=True,
            timeout=120,
        )
        if result.returncode == 0:
            console.print("  [green]Chromium installe[/green]\n")
        else:
            console.print("  [yellow]Chromium deja installe ou erreur (non bloquant)[/yellow]\n")
    except Exception as e:
        console.print(f"  [yellow]Impossible d'installer Chromium: {e}[/yellow]\n")

    # ── Step 4: Daemon systemd ────────────────────────────────────
    console.print("[bold]4. Daemon (service systeme)[/bold]")
    system = platform.system()

    if system == "Linux" and _has_systemd():
        if click.confirm("  Installer figma-audit comme service systemd ?", default=True):
            _install_systemd_service(env_file, db_path)
    elif system == "Darwin":
        if click.confirm("  Installer figma-audit comme service launchd ?", default=True):
            _install_launchd_service(env_file, db_path)
    else:
        console.print(
            "  [dim]Pas de gestionnaire de service detecte. Utilisez 'figma-audit serve'.[/dim]"
        )

    # ── Done ──────────────────────────────────────────────────────
    console.print("\n[bold green]Setup termine ![/bold green]")
    console.print(f"  Config:    {config_dir}")
    console.print(f"  Database:  {db_path}")
    console.print("  Dashboard: http://127.0.0.1:8321")
    console.print(f"\n  Pour lancer manuellement: figma-audit serve --db {db_path}")


def _has_systemd() -> bool:
    import subprocess

    try:
        result = subprocess.run(["systemctl", "--version"], capture_output=True, timeout=5)
        return result.returncode == 0
    except Exception:
        return False  # systemd not available, not an error


def _install_systemd_service(env_file: Path, db_path: Path) -> None:
    import subprocess
    import sys

    python_path = sys.executable
    service_content = f"""[Unit]
Description=figma-audit web dashboard
After=network.target

[Service]
Type=simple
EnvironmentFile={env_file}
ExecStart={python_path} -m figma_audit serve --host 127.0.0.1 --port 8321 --db {db_path}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""
    service_dir = Path.home() / ".config" / "systemd" / "user"
    service_dir.mkdir(parents=True, exist_ok=True)
    service_path = service_dir / "figma-audit.service"
    service_path.write_text(service_content)

    try:
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True, capture_output=True)
        subprocess.run(
            ["systemctl", "--user", "enable", "figma-audit"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["systemctl", "--user", "start", "figma-audit"],
            check=True,
            capture_output=True,
        )
        # Enable lingering so the service runs without active login session
        import getpass

        subprocess.run(["loginctl", "enable-linger", getpass.getuser()], capture_output=True)
        console.print("  [green]Service installe et demarre[/green]")
        console.print("  [dim]  systemctl --user status figma-audit[/dim]")
        console.print("  [dim]  systemctl --user stop figma-audit[/dim]")
        console.print("  [dim]  journalctl --user -u figma-audit -f[/dim]")
    except subprocess.CalledProcessError as e:
        console.print(f"  [red]Erreur systemd: {e}[/red]")
        console.print(f"  [dim]Service ecrit dans {service_path}[/dim]")


def _install_launchd_service(env_file: Path, db_path: Path) -> None:
    import subprocess
    import sys

    python_path = sys.executable
    label = "com.figma-audit.server"

    # Read env vars for launchd plist
    env_vars = {}
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env_vars[k.strip()] = v.strip()

    env_xml = "\n".join(
        f"        <key>{k}</key>\n        <string>{v}</string>" for k, v in env_vars.items()
    )

    plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python_path}</string>
        <string>-m</string>
        <string>figma_audit</string>
        <string>serve</string>
        <string>--host</string>
        <string>127.0.0.1</string>
        <string>--port</string>
        <string>8321</string>
        <string>--db</string>
        <string>{db_path}</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
{env_xml}
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{Path.home() / ".config" / "figma-audit" / "server.log"}</string>
    <key>StandardErrorPath</key>
    <string>{Path.home() / ".config" / "figma-audit" / "server.err"}</string>
</dict>
</plist>
"""
    plist_dir = Path.home() / "Library" / "LaunchAgents"
    plist_dir.mkdir(parents=True, exist_ok=True)
    plist_path = plist_dir / f"{label}.plist"
    plist_path.write_text(plist_content)

    try:
        subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
        subprocess.run(["launchctl", "load", str(plist_path)], check=True, capture_output=True)
        console.print("  [green]Service installe et demarre[/green]")
        console.print("  [dim]  launchctl list | grep figma-audit[/dim]")
        console.print(f"  [dim]  launchctl unload {plist_path}[/dim]")
    except subprocess.CalledProcessError as e:
        console.print(f"  [red]Erreur launchd: {e}[/red]")
        console.print(f"  [dim]Plist ecrit dans {plist_path}[/dim]")


if __name__ == "__main__":
    cli()
