"""FastAPI application factory."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from figma_audit.api import deps
from fastapi import Request
from fastapi.responses import FileResponse, Response

from figma_audit.api.routes import discrepancies, htmx, projects, runs, screens, web
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
    app.include_router(htmx.router)

    # Web UI routes (must be before static mount)
    app.include_router(web.router)

    # Serve project output files (screenshots)
    @app.get("/files/{slug}/{path:path}")
    def serve_project_file(slug: str, path: str) -> Response:
        from sqlmodel import Session, select
        from figma_audit.db.engine import get_engine
        from figma_audit.db.models import Project

        engine = get_engine(db_path)
        with Session(engine) as session:
            project = session.exec(select(Project).where(Project.slug == slug)).first()
            if not project:
                return Response(status_code=404)
            file_path = Path(project.output_dir).expanduser().resolve() / path
            if not file_path.exists() or not file_path.is_file():
                return Response(status_code=404)
            return FileResponse(file_path)

    # Static files for web UI (htmx, css)
    static_dir = Path(__file__).parent.parent / "web" / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok", "version": "0.1.0"}

    return app
