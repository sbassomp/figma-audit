"""Data models for figma-audit."""

from __future__ import annotations

from pydantic import BaseModel, Field


class Bounds(BaseModel):
    x: float
    y: float
    w: float
    h: float


class FigmaElement(BaseModel):
    type: str
    name: str | None = None
    content: str | None = None
    font_family: str | None = None
    font_size: float | None = None
    font_weight: int | None = None
    letter_spacing: float | None = None
    line_height: float | None = None
    color: str | None = None
    fill: str | None = None
    corner_radius: float | None = None
    bounds: Bounds | None = None


class FigmaScreen(BaseModel):
    id: str
    name: str
    page: str
    width: float
    height: float
    image_path: str | None = None
    background_color: str | None = None
    elements: list[FigmaElement] = Field(default_factory=list)


class FigmaManifest(BaseModel):
    file_key: str
    file_name: str
    screens: list[FigmaScreen] = Field(default_factory=list)


class FileMeta(BaseModel):
    file_key: str
    file_name: str
    last_modified: str | None = None
    version: str | None = None
    downloaded_at: str | None = None


class DesignTokens(BaseModel):
    colors: dict[str, str] = Field(default_factory=dict)
    fonts: dict[str, list[str] | str] = Field(default_factory=dict)
    spacing_scale: list[float] = Field(default_factory=list)
    border_radius: dict[str, float] = Field(default_factory=dict)
