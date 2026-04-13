"""Web UI routes serving Jinja2 templates.

This package was originally a single ~1150-line module. It is now split by
resource into focused sub-modules:

- :mod:`dashboard` — the ``/`` landing page
- :mod:`projects` — project CRUD + screens gallery
- :mod:`uploads` — ZIP/.fig upload handlers and their background processors
- :mod:`runs` — run start, pipeline executor, run detail, comparison view

The package exposes a single ``router`` (``APIRouter``) that aggregates
every sub-module's router. ``_upload_progress`` is also re-exported here so
``htmx.py`` can poll its state without knowing the internal layout.
"""

from __future__ import annotations

from fastapi import APIRouter

from figma_audit.api.routes.web._state import _upload_progress
from figma_audit.api.routes.web.dashboard import router as _dashboard_router
from figma_audit.api.routes.web.projects import router as _projects_router
from figma_audit.api.routes.web.runs import router as _runs_router
from figma_audit.api.routes.web.uploads import router as _uploads_router

router = APIRouter()
router.include_router(_dashboard_router)
router.include_router(_projects_router)
router.include_router(_uploads_router)
router.include_router(_runs_router)

__all__ = ["router", "_upload_progress"]
