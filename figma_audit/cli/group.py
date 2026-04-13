"""The shared Click group + a helper that locates the project config file.

This module exists so each command sub-module can register against the same
``cli`` instance without creating a circular import on ``figma_audit.__main__``.
"""

from __future__ import annotations

from pathlib import Path

import click
from rich.console import Console

console = Console()

# Auto-discover config file in current directory
DEFAULT_CONFIG_NAMES = ["figma-audit.yaml", "figma-audit.yml"]


def _find_config(config_path: str | None) -> Path | None:
    """Return the path to the project config file, or ``None`` if not found."""
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
