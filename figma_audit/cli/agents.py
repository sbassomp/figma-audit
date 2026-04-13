"""Interactive agent commands: ``setup-test-data``."""

from __future__ import annotations

import click

from figma_audit.cli.group import _find_config, cli, console
from figma_audit.config import Config
from figma_audit.utils.checks import check_api_keys, load_env_file


@cli.command(name="setup-test-data")
@click.option("--project", "-p", default=None, help="Path to the project to analyze")
@click.option("--output", "-o", default=None, help="Output directory")
@click.option("--config", "config_path", type=click.Path(exists=True), help="Config YAML file")
def setup_test_data(
    project: str | None,
    output: str | None,
    config_path: str | None,
) -> None:
    """Interactive agent that explores your code and validates API payloads.

    Reads pages_manifest.json, finds the correct request DTOs in your codebase,
    builds payloads, tests them against the live backend, and writes a validated
    test_setup block to figma-audit.yaml. Requires an interactive terminal.
    """
    load_env_file()
    check_api_keys()
    cfg = Config.load(
        config_path=_find_config(config_path),
        project=project,
        output=output,
    )

    from figma_audit.phases.setup_test_data import run

    yaml_path = run(cfg)
    console.print(f"\n[bold]Done. Config updated: {yaml_path}[/bold]")
