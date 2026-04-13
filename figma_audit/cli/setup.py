"""``figma-audit setup`` — interactive first-run setup + daemon installation."""

from __future__ import annotations

from pathlib import Path

import click

from figma_audit.cli.group import cli, console


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
    console.print("[bold]1. API Keys[/bold]")
    existing_env = {}
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                existing_env[k.strip()] = v.strip()

    anthropic_key = existing_env.get("ANTHROPIC_API_KEY", os.environ.get("ANTHROPIC_API_KEY", ""))
    figma_token = existing_env.get("FIGMA_TOKEN", os.environ.get("FIGMA_TOKEN", ""))

    api_status = "[green]configured[/green]" if anthropic_key else "[red]missing[/red]"
    figma_status = "[green]configured[/green]" if figma_token else "[red]missing[/red]"
    console.print(f"  ANTHROPIC_API_KEY: {api_status}")
    console.print(f"  FIGMA_TOKEN:       {figma_status}")

    if not anthropic_key or click.confirm("  Update ANTHROPIC_API_KEY?", default=not anthropic_key):
        anthropic_key = click.prompt("  ANTHROPIC_API_KEY", default=anthropic_key)

    if not figma_token or click.confirm("  Update FIGMA_TOKEN?", default=not figma_token):
        figma_token = click.prompt("  FIGMA_TOKEN", default=figma_token)

    env_content = f"ANTHROPIC_API_KEY={anthropic_key}\nFIGMA_TOKEN={figma_token}\n"
    env_file.write_text(env_content)
    env_file.chmod(0o600)
    console.print(f"  [green]Keys saved to {env_file}[/green]\n")

    # ── Step 2: Database ──────────────────────────────────────────
    console.print("[bold]2. Database[/bold]")
    from figma_audit.db.engine import init_db

    init_db(str(db_path))
    console.print(f"  [green]DB initialized: {db_path}[/green]\n")

    # ── Step 3: Playwright ────────────────────────────────────────
    console.print("[bold]3. Browser (Playwright)[/bold]")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            capture_output=True,
            timeout=120,
        )
        if result.returncode == 0:
            console.print("  [green]Chromium installed[/green]\n")
        else:
            console.print("  [yellow]Chromium already installed or error (non-blocking)[/yellow]\n")
    except Exception as e:
        console.print(f"  [yellow]Could not install Chromium: {e}[/yellow]\n")

    # ── Step 4: Daemon systemd ────────────────────────────────────
    console.print("[bold]4. Daemon (system service)[/bold]")
    system = platform.system()

    if system == "Linux" and _has_systemd():
        if click.confirm("  Install figma-audit as a systemd service?", default=True):
            _install_systemd_service(env_file, db_path)
    elif system == "Darwin":
        if click.confirm("  Install figma-audit as a launchd service?", default=True):
            _install_launchd_service(env_file, db_path)
    else:
        console.print("  [dim]No service manager detected. Use 'figma-audit serve'.[/dim]")

    # ── Done ──────────────────────────────────────────────────────
    console.print("\n[bold green]Setup complete![/bold green]")
    console.print(f"  Config:    {config_dir}")
    console.print(f"  Database:  {db_path}")
    console.print("  Dashboard: http://127.0.0.1:8321")
    console.print(f"\n  To run manually: figma-audit serve --db {db_path}")


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
        console.print("  [green]Service installed and started[/green]")
        console.print("  [dim]  systemctl --user status figma-audit[/dim]")
        console.print("  [dim]  systemctl --user stop figma-audit[/dim]")
        console.print("  [dim]  journalctl --user -u figma-audit -f[/dim]")
    except subprocess.CalledProcessError as e:
        console.print(f"  [red]systemd error: {e}[/red]")
        console.print(f"  [dim]Service written to {service_path}[/dim]")


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
        console.print("  [green]Service installed and started[/green]")
        console.print("  [dim]  launchctl list | grep figma-audit[/dim]")
        console.print(f"  [dim]  launchctl unload {plist_path}[/dim]")
    except subprocess.CalledProcessError as e:
        console.print(f"  [red]launchd error: {e}[/red]")
        console.print(f"  [dim]Plist written to {plist_path}[/dim]")
