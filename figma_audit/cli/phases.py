"""Per-phase CLI commands: figma, analyze, match, capture, compare, report.

Each command is a thin wrapper around the corresponding ``phases/*`` module's
``run()`` function. The full pipeline is in :mod:`figma_audit.cli.run`.
"""

from __future__ import annotations

import click

from figma_audit.cli.group import _find_config, cli, console
from figma_audit.config import Config
from figma_audit.utils.checks import load_env_file


@cli.command()
@click.option("--figma-url", envvar="FIGMA_URL", help="Figma file URL")
@click.option("--figma-file", type=click.Path(exists=True), help="Local .fig file (offline)")
@click.option("--figma-token", envvar="FIGMA_TOKEN", help="Figma API token")
@click.option("--output", "-o", default=None, help="Output directory")
@click.option("--config", "config_path", type=click.Path(exists=True), help="Config YAML file")
@click.option("--force-refresh", is_flag=True, help="Force re-download from Figma API")
@click.option("--offline", is_flag=True, help="Work from local cache only, no API calls")
@click.option("--target-page", help="Figma page ID to focus on (e.g. '45:927')")
def figma(
    figma_url: str | None,
    figma_file: str | None,
    figma_token: str | None,
    output: str | None,
    config_path: str | None,
    force_refresh: bool,
    offline: bool,
    target_page: str | None,
) -> None:
    """Phase 2: Export Figma file -- download tree, extract tokens, export PNGs."""
    if figma_file and figma_url:
        raise click.UsageError("Use either --figma-file or --figma-url, not both.")
    cfg = Config.load(
        config_path=_find_config(config_path),
        figma_url=figma_url,
        figma_file=figma_file,
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
@click.option(
    "--agentic",
    is_flag=True,
    help="Agentic mode: Claude explores the codebase with tools.",
)
def analyze(
    project: str | None,
    output: str | None,
    config_path: str | None,
    agentic: bool,
) -> None:
    """Phase 1: Analyze project code -- detect framework, extract routes, produce manifest."""
    load_env_file()
    cfg = Config.load(
        config_path=_find_config(config_path),
        project=project,
        output=output,
    )
    if agentic:
        cfg.analyze_mode = "agentic"

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
