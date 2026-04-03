"""Configuration for figma-audit."""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, model_validator


class FigmaConfig(BaseModel):
    cache_dir: str = "figma_raw"
    request_delay: float = 3.0
    batch_size: int = 8
    retry_wait_default: int = 60
    max_retries: int = 5


class ViewportConfig(BaseModel):
    width: int = 390
    height: int = 844
    device_scale_factor: int = 2


class SeedAccountConfig(BaseModel):
    email: str | None = None
    otp: str = "1234"


class ThresholdsConfig(BaseModel):
    color_delta_e: float = 5.0
    font_size_tolerance: int = 2
    spacing_tolerance: int = 4


class Config(BaseModel):
    project: str | None = None
    figma_url: str | None = None
    figma_token: str | None = None
    app_url: str | None = None
    anthropic_api_key: str | None = None
    output: str = "./audit-results"
    figma: FigmaConfig = Field(default_factory=FigmaConfig)
    viewport: ViewportConfig = Field(default_factory=ViewportConfig)
    thresholds: ThresholdsConfig = Field(default_factory=ThresholdsConfig)
    seed_account: SeedAccountConfig = Field(default_factory=SeedAccountConfig)
    include_routes: list[str] = Field(default_factory=list)
    exclude_routes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def resolve_env_vars(self) -> Config:
        """Resolve ${ENV_VAR} references in string fields."""
        for field_name in ("figma_token", "anthropic_api_key"):
            value = getattr(self, field_name)
            if value and value.startswith("${") and value.endswith("}"):
                env_key = value[2:-1]
                setattr(self, field_name, os.environ.get(env_key))
        return self

    @property
    def output_dir(self) -> Path:
        return Path(self.output).expanduser().resolve()

    @property
    def figma_cache_dir(self) -> Path:
        return self.output_dir / self.figma.cache_dir

    @property
    def figma_screens_dir(self) -> Path:
        return self.output_dir / "figma_screens"

    @property
    def figma_file_key(self) -> str | None:
        if not self.figma_url:
            return None
        # Extract file key from URL: .../design/<key>/... or .../file/<key>/...
        parts = self.figma_url.split("/")
        for i, part in enumerate(parts):
            if part in ("design", "file") and i + 1 < len(parts):
                return parts[i + 1]
        return None

    @staticmethod
    def load(config_path: Path | None = None, **overrides: str | None) -> Config:
        """Load config from YAML file, env vars, and CLI overrides."""
        data: dict = {}
        if config_path and config_path.exists():
            with open(config_path) as f:
                data = yaml.safe_load(f) or {}

        # CLI overrides take precedence
        for key, value in overrides.items():
            if value is not None:
                data[key] = value

        # Env var fallbacks
        if not data.get("figma_token"):
            data["figma_token"] = os.environ.get("FIGMA_TOKEN")
        if not data.get("anthropic_api_key"):
            data["anthropic_api_key"] = os.environ.get("ANTHROPIC_API_KEY")

        return Config(**data)
