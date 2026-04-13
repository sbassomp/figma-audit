"""``figma-audit serve`` — start the FastAPI dashboard."""

from __future__ import annotations

import click

from figma_audit.cli.group import cli, console
from figma_audit.utils.checks import load_env_file


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
