"""Shared state and helpers for the web/* sub-routers.

Holds the Jinja2 ``templates`` object, the per-project ``_upload_progress``
dict, and the ``_nav_projects`` helper used by every page that renders a
sidebar.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from figma_audit import get_build_info
from figma_audit.db.models import Project

_templates_dir = Path(__file__).parent.parent.parent.parent / "web" / "templates"
templates = Jinja2Templates(directory=str(_templates_dir))

# Inject build version into all templates
templates.env.globals["build_version"] = get_build_info()


# Global upload progress state, keyed by project slug (or slug + "_fig" for
# the .fig flow). Read by the sub-routers and by htmx.py via re-export from
# the package __init__.
_upload_progress: dict[str, dict] = {}


def _nav_projects(session: Session) -> list[dict]:
    """Build the sidebar project list rendered by every page."""
    projects = session.exec(select(Project).order_by(Project.name)).all()
    return [{"name": p.name, "slug": p.slug} for p in projects]
