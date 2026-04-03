"""FastAPI application factory."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from figma_audit.api import deps
from figma_audit.api.routes import discrepancies, projects, runs, screens
from figma_audit.db.engine import init_db


def create_app(db_path: str = "figma-audit.db") -> FastAPI:
    """Create and configure the FastAPI application."""
    deps.set_db_path(db_path)
    init_db(db_path)

    app = FastAPI(
        title="figma-audit",
        description="Semantic comparison between Figma designs and deployed web apps",
        version="0.1.0",
    )

    # API routes
    app.include_router(projects.router)
    app.include_router(runs.router)
    app.include_router(screens.router)
    app.include_router(discrepancies.router)

    # Static files for web UI (htmx, css)
    static_dir = Path(__file__).parent.parent / "web" / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok", "version": "0.1.0"}

    return app
