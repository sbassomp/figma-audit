"""Pre-flight checks before running phases."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from rich.console import Console

console = Console()


def check_playwright_browser() -> bool:
    """Check if Playwright Chromium is installed. Returns True if OK."""
    # Playwright stores browsers in ~/.cache/ms-playwright/
    cache_dir = Path.home() / ".cache" / "ms-playwright"
    chromium_dirs = list(cache_dir.glob("chromium-*")) if cache_dir.exists() else []

    for d in chromium_dirs:
        if (d / "INSTALLATION_COMPLETE").exists():
            return True

    console.print("[red]Chromium n'est pas installe pour Playwright.[/red]")
    console.print("Lancez: [bold]figma-audit setup[/bold] ou [bold]playwright install chromium[/bold]")
    return False


def check_api_keys() -> bool:
    """Check if required API keys are available. Returns True if OK."""
    # Check env vars first
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    if anthropic_key:
        return True

    # Check config file
    env_file = Path.home() / ".config" / "figma-audit" / "env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.startswith("ANTHROPIC_API_KEY=") and len(line) > 20:
                return True

    console.print("[red]ANTHROPIC_API_KEY non configuree.[/red]")
    console.print("Lancez: [bold]figma-audit setup[/bold] ou exportez ANTHROPIC_API_KEY")
    return False


def load_env_file() -> None:
    """Load API keys from ~/.config/figma-audit/env into os.environ if not already set."""
    env_file = Path.home() / ".config" / "figma-audit" / "env"
    if not env_file.exists():
        return

    for line in env_file.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if key and value and key not in os.environ:
                os.environ[key] = value
