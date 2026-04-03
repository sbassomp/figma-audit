"""CLI entry point for figma-audit."""

from __future__ import annotations

from pathlib import Path

import click
from rich.console import Console

from figma_audit.config import Config

console = Console()


@click.group()
@click.version_option(package_name="figma-audit")
def cli() -> None:
    """figma-audit: Semantic comparison between Figma designs and deployed web apps."""


@cli.command()
@click.option("--figma-url", envvar="FIGMA_URL", help="Figma file URL")
@click.option("--figma-token", envvar="FIGMA_TOKEN", help="Figma API token")
@click.option("--output", "-o", default="./audit-results", help="Output directory")
@click.option("--config", "config_path", type=click.Path(exists=True), help="Config YAML file")
@click.option("--force-refresh", is_flag=True, help="Force re-download from Figma API")
@click.option("--offline", is_flag=True, help="Work from local cache only, no API calls")
@click.option("--target-page", help="Figma page ID to focus on (e.g. '45:927')")
def figma(
    figma_url: str | None,
    figma_token: str | None,
    output: str,
    config_path: str | None,
    force_refresh: bool,
    offline: bool,
    target_page: str | None,
) -> None:
    """Phase 2: Export Figma file — download tree, extract tokens, export PNGs."""
    cfg = Config.load(
        config_path=Path(config_path) if config_path else None,
        figma_url=figma_url,
        figma_token=figma_token,
        output=output,
    )

    from figma_audit.phases.export_figma import run

    manifest_path = run(cfg, force_refresh=force_refresh, offline=offline, target_page=target_page)
    console.print(f"\n[bold]Done. Manifest: {manifest_path}[/bold]")


@cli.command()
@click.option("--project", "-p", required=True, help="Path to the project to analyze")
@click.option("--output", "-o", default="./audit-results", help="Output directory")
@click.option("--config", "config_path", type=click.Path(exists=True), help="Config YAML file")
def analyze(
    project: str,
    output: str,
    config_path: str | None,
) -> None:
    """Phase 1: Analyze project code — detect framework, extract routes, produce manifest."""
    cfg = Config.load(
        config_path=Path(config_path) if config_path else None,
        project=project,
        output=output,
    )

    from figma_audit.phases.analyze_code import run

    manifest_path = run(cfg)
    console.print(f"\n[bold]Done. Manifest: {manifest_path}[/bold]")


@cli.command()
@click.option("--output", "-o", default="./audit-results", help="Output directory")
@click.option("--config", "config_path", type=click.Path(exists=True), help="Config YAML file")
def match(
    output: str,
    config_path: str | None,
) -> None:
    """Phase 3: Match Figma screens to application routes using AI Vision."""
    cfg = Config.load(
        config_path=Path(config_path) if config_path else None,
        output=output,
    )

    from figma_audit.phases.match_screens import run

    mapping_path = run(cfg)
    console.print(f"\n[bold]Done. Mapping: {mapping_path}[/bold]")


@cli.command()
@click.option("--app-url", envvar="APP_URL", required=True, help="Deployed app URL")
@click.option("--output", "-o", default="./audit-results", help="Output directory")
@click.option("--config", "config_path", type=click.Path(exists=True), help="Config YAML file")
def capture(
    app_url: str,
    output: str,
    config_path: str | None,
) -> None:
    """Phase 4: Capture app screenshots via Playwright."""
    cfg = Config.load(
        config_path=Path(config_path) if config_path else None,
        app_url=app_url,
        output=output,
    )

    from figma_audit.phases.capture_app import run

    captures_path = run(cfg)
    console.print(f"\n[bold]Done. Captures: {captures_path}[/bold]")


@cli.command()
@click.option("--output", "-o", default="./audit-results", help="Output directory")
@click.option("--config", "config_path", type=click.Path(exists=True), help="Config YAML file")
def compare(
    output: str,
    config_path: str | None,
) -> None:
    """Phase 5: Compare Figma designs against app screenshots."""
    cfg = Config.load(
        config_path=Path(config_path) if config_path else None,
        output=output,
    )

    from figma_audit.phases.compare import run

    discrepancies_path = run(cfg)
    console.print(f"\n[bold]Done. Discrepancies: {discrepancies_path}[/bold]")


@cli.command()
@click.option("--output", "-o", default="./audit-results", help="Output directory")
@click.option("--config", "config_path", type=click.Path(exists=True), help="Config YAML file")
def report(
    output: str,
    config_path: str | None,
) -> None:
    """Phase 6: Generate standalone HTML report."""
    cfg = Config.load(
        config_path=Path(config_path) if config_path else None,
        output=output,
    )

    from figma_audit.phases.report import run

    report_path = run(cfg)
    console.print(f"\n[bold]Done. Report: {report_path}[/bold]")


if __name__ == "__main__":
    cli()
