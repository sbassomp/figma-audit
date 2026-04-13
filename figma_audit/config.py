"""Configuration for figma-audit."""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


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


class TestCredentials(BaseModel):
    __test__ = False  # pytest: not a test class
    email: str | None = None
    otp: str = "1234"


class SeedAccountConfig(BaseModel):
    email: str | None = None
    otp: str = "1234"


class ThresholdsConfig(BaseModel):
    color_delta_e: float = 5.0
    font_size_tolerance: int = 2
    spacing_tolerance: int = 4


class Account(BaseModel):
    """A named user profile used by test_setup steps and per-page viewers.

    Accounts live inside ``TestSetup.accounts`` keyed by a role name that
    steps and pages reference (e.g. ``requester``, ``taker``). The role name
    is the dict key — this model only carries the credentials.
    """

    email: str | None = None
    otp: str = "1234"
    # Per-account auth payload override. Rarely needed — most apps use one
    # login flow. Set this when a specific role logs in via a different
    # endpoint or with different fields (e.g. drivers have a separate API).
    auth_payload: dict | None = None


class SetupStep(BaseModel):
    """One HTTP call executed before capture to seed backend state.

    Steps form a DAG via ``depends_on`` and run in topological order. Each
    step is executed with the bearer token of the account named by
    ``as_role`` (spelled ``as`` in YAML). Response values can be extracted
    via ``save`` and templated into later steps or page URLs using
    ``${key}``.
    """

    model_config = ConfigDict(populate_by_name=True)

    name: str
    as_role: str = Field(alias="as")
    endpoint: str
    method: str = "POST"
    payload: dict = Field(default_factory=dict)
    # Map of test_data key → dotted JSONPath into the response body.
    # Example: ``{"course_id": "id"}`` stores ``response["id"]`` as ``course_id``.
    save: dict[str, str] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)


class TestSetup(BaseModel):
    """Declarative, multi-actor test data setup.

    Replaces the historical single-account shape (``seed_items`` + ``take_item``)
    with an explicit ``accounts`` map and a DAG of ``steps``. Legacy shapes
    are auto-migrated by :meth:`from_raw`.
    """

    # Opt out of pytest collection (class name starts with "Test").
    __test__ = False

    # Shared auth flow — all accounts use the same login endpoint and
    # token extraction path. An account may override ``auth_payload`` if it
    # needs different login fields.
    auth_endpoint: str | None = None
    auth_otp_request_endpoint: str | None = None
    auth_payload: dict = Field(default_factory=dict)
    auth_token_path: str = "accessToken"

    accounts: dict[str, Account] = Field(default_factory=dict)
    steps: list[SetupStep] = Field(default_factory=list)
    # Account role used to load pages that do not specify their own viewer.
    default_viewer: str | None = None

    cleanup_endpoint: str | None = None

    @model_validator(mode="after")
    def _validate_refs(self) -> TestSetup:
        known_roles = set(self.accounts.keys())
        step_names = [s.name for s in self.steps]
        if len(step_names) != len(set(step_names)):
            raise ValueError("Duplicate step names in test_setup.steps")
        name_set = set(step_names)

        for step in self.steps:
            if step.as_role not in known_roles:
                known = sorted(known_roles) or "none"
                raise ValueError(
                    f"Step '{step.name}' uses unknown role '{step.as_role}'. "
                    f"Declare it under test_setup.accounts (known: {known})."
                )
            for dep in step.depends_on:
                if dep not in name_set:
                    raise ValueError(f"Step '{step.name}' depends on unknown step '{dep}'.")

        # Cycle detection — Kahn's algorithm on the depends_on edge set.
        incoming = {s.name: set(s.depends_on) for s in self.steps}
        ready = sorted(n for n, deps in incoming.items() if not deps)
        visited = 0
        while ready:
            node = ready.pop(0)
            visited += 1
            for other, deps in incoming.items():
                if node in deps:
                    deps.discard(node)
                    if not deps:
                        ready.append(other)
                        ready.sort()
        if visited != len(self.steps):
            raise ValueError("Cycle detected in test_setup.steps depends_on")

        if self.default_viewer and self.default_viewer not in known_roles:
            raise ValueError(f"default_viewer '{self.default_viewer}' is not a declared account")
        return self

    def topological_order(self) -> list[SetupStep]:
        """Return steps in a valid execution order (depends_on respected).

        Determinism: ties are broken by step name so the same config always
        produces the same ordering (matters for reproducible runs and tests).
        """
        by_name = {s.name: s for s in self.steps}
        incoming = {s.name: set(s.depends_on) for s in self.steps}
        ordered: list[SetupStep] = []
        ready = sorted(n for n, deps in incoming.items() if not deps)
        while ready:
            node = ready.pop(0)
            ordered.append(by_name[node])
            for other in list(incoming):
                if node in incoming[other]:
                    incoming[other].discard(node)
                    if not incoming[other]:
                        ready.append(other)
                        ready.sort()
        return ordered

    @classmethod
    def from_raw(
        cls,
        data: dict | None,
        *,
        main_credentials: dict | None = None,
        seed_credentials: dict | None = None,
    ) -> TestSetup:
        """Parse a raw test_setup dict, auto-migrating the legacy shape.

        Accepts two input shapes:

        - **New multi-actor**: the dict already contains ``accounts`` and/or
          ``steps``. Parsed as-is (runtime keys like ``_api_prefix_hint`` are
          stripped). Credentials passed via ``main_credentials`` /
          ``seed_credentials`` are ignored — the new shape is self-sufficient.

        - **Legacy mono-actor**: the historic shape with ``seed_items`` and
          optional ``take_item``. Rewritten to ``accounts`` (``seed`` and/or
          ``main``, populated from the passed credentials) and a linear chain
          of steps (one per seed_item as role ``seed``; ``take_item`` as role
          ``main`` if present).
        """
        if not data:
            data = {}

        if data.get("accounts") or data.get("steps"):
            clean = {k: v for k, v in data.items() if not k.startswith("_")}
            return cls.model_validate(clean)

        referenced: set[str] = set()
        if data.get("seed_items"):
            referenced.add("seed")
        if data.get("take_item"):
            referenced.add("main")

        accounts: dict[str, Account] = {}
        for role in referenced:
            source = seed_credentials if role == "seed" else main_credentials
            if source and source.get("email"):
                accounts[role] = Account(
                    email=source.get("email"),
                    otp=source.get("otp", "1234"),
                )
            else:
                # Stub — credentials will be resolved from config at run time.
                accounts[role] = Account()

        steps: list[SetupStep] = []
        prev_name: str | None = None
        for i, item in enumerate(data.get("seed_items") or []):
            default_name = item.get("test_data_key") or f"seed_{i}"
            save_map: dict[str, str] = {}
            if item.get("test_data_key"):
                save_map[item["test_data_key"]] = item.get("id_path", "id")
            steps.append(
                SetupStep(
                    name=default_name,
                    **{"as": "seed"},
                    endpoint=item.get("endpoint", ""),
                    method=(item.get("method") or "POST").upper(),
                    payload=item.get("payload") or {},
                    save=save_map,
                    depends_on=[prev_name] if prev_name else [],
                )
            )
            prev_name = default_name

        take = data.get("take_item")
        if take:
            take_name = take.get("test_data_key") or "take"
            save_map: dict[str, str] = {}
            if take.get("test_data_key"):
                save_map[take["test_data_key"]] = take.get("id_path", "id")
            steps.append(
                SetupStep(
                    name=take_name,
                    **{"as": "main"},
                    endpoint=take.get("endpoint", ""),
                    method=(take.get("method") or "POST").upper(),
                    payload=take.get("payload") or {},
                    save=save_map,
                    depends_on=[prev_name] if prev_name else [],
                )
            )

        default_viewer = "main" if "main" in accounts else ("seed" if "seed" in accounts else None)

        return cls(
            auth_endpoint=data.get("auth_endpoint"),
            auth_otp_request_endpoint=data.get("auth_otp_request_endpoint"),
            auth_payload=data.get("auth_payload") or {},
            auth_token_path=data.get("auth_token_path", "accessToken"),
            accounts=accounts,
            steps=steps,
            default_viewer=default_viewer,
            cleanup_endpoint=data.get("cleanup_endpoint"),
        )


class Config(BaseModel):
    project: str | None = None
    figma_url: str | None = None
    figma_file: str | None = None
    figma_token: str | None = None
    app_url: str | None = None
    anthropic_api_key: str | None = None
    output: str = "./audit-results"
    figma: FigmaConfig = Field(default_factory=FigmaConfig)
    viewport: ViewportConfig = Field(default_factory=ViewportConfig)
    thresholds: ThresholdsConfig = Field(default_factory=ThresholdsConfig)
    test_credentials: TestCredentials = Field(default_factory=TestCredentials)
    seed_account: SeedAccountConfig = Field(default_factory=SeedAccountConfig)
    test_setup: dict = Field(default_factory=dict)
    analyze_mode: str = "one-shot"  # "one-shot" | "agentic"
    analyze_model: str | None = None  # Override model for Phase 1 (e.g. "claude-opus-4-6")
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
    def figma_file_path(self) -> Path | None:
        """Resolved path to local .fig file, if configured."""
        if not self.figma_file:
            return None
        return Path(self.figma_file).expanduser().resolve()

    @property
    def figma_file_key(self) -> str | None:
        """Extract file key from Figma URL, or derive from .fig filename."""
        if self.figma_url:
            parts = self.figma_url.split("/")
            for i, part in enumerate(parts):
                if part in ("design", "file") and i + 1 < len(parts):
                    return parts[i + 1]
        if self.figma_file:
            return Path(self.figma_file).stem
        return None

    def test_setup_model(self) -> TestSetup:
        """Return ``self.test_setup`` parsed as a validated :class:`TestSetup`.

        Legacy mono-actor shapes are auto-migrated, pulling account
        credentials from :attr:`seed_account` and :attr:`test_credentials`.
        Callers that don't yet consume the new model can keep reading
        ``self.test_setup`` as a raw dict — this accessor is additive.
        """
        seed_creds: dict | None = None
        if self.seed_account.email:
            seed_creds = {
                "email": self.seed_account.email,
                "otp": self.seed_account.otp,
            }
        main_creds: dict | None = None
        if self.test_credentials.email:
            main_creds = {
                "email": self.test_credentials.email,
                "otp": self.test_credentials.otp,
            }
        return TestSetup.from_raw(
            self.test_setup,
            main_credentials=main_creds,
            seed_credentials=seed_creds,
        )

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
