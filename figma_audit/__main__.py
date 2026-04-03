"""CLI entry point for figma-audit."""

from __future__ import annotations

from pathlib import Path

import click
from rich.console import Console

from figma_audit.config import Config

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
@click.version_option(package_name="figma-audit")
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
    cfg = Config.load(config_path=_find_config(config_path))

    phases = PHASE_ORDER
    if from_phase:
        idx = phases.index(from_phase)
        phases = phases[idx:]
        console.print(f"[bold]Resuming from {PHASE_NAMES[from_phase]}[/bold]\n")
    else:
        console.print("[bold]Running full audit pipeline[/bold]\n")

    for phase_name in phases:
        console.print(f"\n{'='*60}")
        console.print(f"[bold]{PHASE_NAMES[phase_name]}[/bold]")
        console.print(f"{'='*60}\n")

        if phase_name == "analyze":
            from figma_audit.phases.analyze_code import run as run_analyze
            run_analyze(cfg)

        elif phase_name == "figma":
            from figma_audit.phases.export_figma import run as run_figma
            run_figma(cfg, offline=offline, target_page=target_page)

        elif phase_name == "match":
            from figma_audit.phases.match_screens import run as run_match
            mapping_path = run_match(cfg)
            # Auto-verify for pipeline mode
            import yaml
            with open(mapping_path) as f:
                data = yaml.safe_load(f)
            if not data.get("verified"):
                data["verified"] = True
                with open(mapping_path, "w") as f:
                    yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
                console.print("[yellow]Auto-verified mapping for pipeline mode[/yellow]")

        elif phase_name == "capture":
            from figma_audit.phases.capture_app import run as run_capture
            run_capture(cfg)

        elif phase_name == "compare":
            from figma_audit.phases.compare import run as run_compare
            run_compare(cfg)

        elif phase_name == "report":
            from figma_audit.phases.report import run as run_report
            report_path = run_report(cfg)
            console.print(f"\n[bold green]Pipeline complete! Report: {report_path}[/bold green]")


@cli.command()
@click.option("--host", default="127.0.0.1", help="Bind host")
@click.option("--port", default=8321, type=int, help="Bind port")
@click.option("--db", "db_path", default="figma-audit.db", help="SQLite database path")
def serve(host: str, port: int, db_path: str) -> None:
    """Start the figma-audit web server (API + dashboard)."""
    import uvicorn

    from figma_audit.api.app import create_app

    app = create_app(db_path=db_path)
    console.print(f"[bold]Starting figma-audit server on http://{host}:{port}[/bold]")
    console.print(f"  Database: {db_path}")
    console.print(f"  API docs: http://{host}:{port}/docs")
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    cli()
