"""SQLModel database tables for figma-audit."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Project(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    slug: str = Field(unique=True, index=True)
    figma_url: str | None = None
    app_url: str | None = None
    project_path: str | None = None
    config_yaml: str | None = None
    output_dir: str = "./output"
    test_email: str | None = None
    test_otp: str = "1234"
    seed_email: str | None = None
    seed_otp: str = "1234"
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class Run(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="project.id", index=True)
    status: str = "pending"  # pending | running | completed | failed
    current_phase: str | None = None
    from_phase: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    stats_json: str | None = None  # JSON serialized stats
    progress_json: str | None = None  # JSON: live progress data for web UI
    error: str | None = None
    created_at: datetime = Field(default_factory=_now)


class Screen(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="project.id", index=True)
    figma_node_id: str
    name: str
    page: str = ""
    width: float = 0
    height: float = 0
    image_path: str | None = None  # relative to project output_dir
    status: str = "current"  # current | obsolete | draft | component
    mapped_route: str | None = None
    mapped_page_id: str | None = None
    mapping_confidence: float | None = None
    metadata_json: str | None = None  # JSON: elements, background_color, etc.
    created_at: datetime = Field(default_factory=_now)


class Capture(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    run_id: int = Field(foreign_key="run.id", index=True)
    page_id: str
    route: str
    landed_url: str | None = None  # Final URL after navigation (with params resolved)
    # Which account role was logged into the browser when this page was
    # captured. Populated by Phase 4 from the test_setup default_viewer
    # or the page's explicit ``viewer`` override. None for legacy runs and
    # public/unauthenticated captures.
    viewer_role: str | None = None
    screenshot_path: str | None = None
    styles_available: bool = False
    error: str | None = None
    created_at: datetime = Field(default_factory=_now)


class Discrepancy(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    run_id: int = Field(foreign_key="run.id", index=True)
    screen_id: int | None = Field(default=None, foreign_key="screen.id")
    page_id: str
    route: str
    category: str
    severity: str  # critical | important | minor
    description: str
    figma_value: str | None = None
    app_value: str | None = None
    location: str | None = None
    status: str = "open"  # open | ignored | acknowledged | fixed | wontfix
    overall_fidelity: str | None = None
    created_at: datetime = Field(default_factory=_now)


class Annotation(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    discrepancy_id: int | None = Field(default=None, foreign_key="discrepancy.id", index=True)
    screen_id: int | None = Field(default=None, foreign_key="screen.id", index=True)
    author: str = "user"
    content: str
    created_at: datetime = Field(default_factory=_now)
