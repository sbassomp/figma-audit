"""Phase 1: Analyze Code — Detect framework, extract routes, produce pages manifest.

This package was originally a single ~700-line module. It is now split into
focused sub-modules:

- :mod:`discovery` — framework detection, file globbing, pure helpers
  (``_detect_framework``, ``_find_files``, ``_read_file_safe``, all the
  ``*_PATTERNS`` constants)
- :mod:`one_shot` — the default mode: ``_build_prompt``, ``SYSTEM_PROMPT``,
  ``_run_one_shot``
- :mod:`agentic` — opt-in mode: ``AGENTIC_SYSTEM_PROMPT``,
  ``_build_agentic_seed_message``, ``_run_agentic``

The module-level ``_last_client`` lives here so cost-tracking callers
(``figma_audit.api.routes.web``, ``figma_audit.__main__``) can read it via
``getattr(figma_audit.phases.analyze_code, "_last_client", None)``. The
sub-modules write to it via ``import figma_audit.phases.analyze_code as _pkg;
_pkg._last_client = client``.
"""

from __future__ import annotations

import os

from rich.console import Console

from figma_audit.config import Config
from figma_audit.phases.analyze_code.agentic import (
    AGENTIC_SYSTEM_PROMPT,
    _build_agentic_seed_message,
    _run_agentic,
)
from figma_audit.phases.analyze_code.discovery import (
    API_PATTERNS,
    FRAMEWORK_MARKERS,
    MAX_FILE_SIZE,
    MAX_TOTAL_PROMPT_SIZE,
    PAGE_PATTERNS,
    ROUTER_PATTERNS,
    TOKEN_PATTERNS,
    _detect_framework,
    _find_files,
    _read_file_safe,
)
from figma_audit.phases.analyze_code.one_shot import (
    SYSTEM_PROMPT,
    _build_prompt,
    _run_one_shot,
)
from figma_audit.utils.claude_client import ClaudeClient

console = Console()

# Exposed after run() for cost tracking by callers. Sub-modules write to
# this attribute via ``import figma_audit.phases.analyze_code as _pkg``.
_last_client: ClaudeClient | None = None


def run(config: Config):
    """Run Phase 1: Analyze the project codebase and produce pages_manifest.json.

    Two modes:

    - **one-shot** (default): sends all source code in a single prompt.
      Fast, cheap (~$0.31).
    - **agentic** (opt-in): Claude explores the codebase with read_file/grep_code
      tools. More expensive (~$0.55-1.50) but finds correct DTOs, auth guards,
      and endpoints because it can read exactly what it needs instead of guessing
      from a dump.

    Activate agentic mode via:

    - CLI: ``figma-audit analyze --agentic`` or ``figma-audit run --agentic``
    - YAML: ``analyze_mode: agentic``
    - Env: ``FIGMA_AUDIT_ANALYZE_MODE=agentic``

    Returns the path to the generated ``pages_manifest.json``.
    """
    mode = (
        os.environ.get("FIGMA_AUDIT_ANALYZE_MODE")
        or getattr(config, "analyze_mode", "one-shot")
        or "one-shot"
    )
    if mode == "agentic":
        console.print("[bold cyan]Mode: agentic (agent explores codebase with tools)[/bold cyan]")
        return _run_agentic(config)
    return _run_one_shot(config)


__all__ = [
    # Public API
    "run",
    "_last_client",
    # Discovery helpers (used by tests)
    "API_PATTERNS",
    "FRAMEWORK_MARKERS",
    "MAX_FILE_SIZE",
    "MAX_TOTAL_PROMPT_SIZE",
    "PAGE_PATTERNS",
    "ROUTER_PATTERNS",
    "TOKEN_PATTERNS",
    "_detect_framework",
    "_find_files",
    "_read_file_safe",
    # One-shot path
    "SYSTEM_PROMPT",
    "_build_prompt",
    "_run_one_shot",
    # Agentic path
    "AGENTIC_SYSTEM_PROMPT",
    "_build_agentic_seed_message",
    "_run_agentic",
]
