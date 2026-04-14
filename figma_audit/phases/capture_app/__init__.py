"""Phase 4: Capture App — Navigate deployed app with Playwright, take screenshots.

This package was originally a single ~1000-line module. It is now split into
focused sub-modules:

- :mod:`templates` — template substitution (``${test_data.X}``, ``${now+1d}``),
  placeholder guard, slug helper.
- :mod:`api_client` — login + seed-data API helpers (``_api_login``,
  ``_setup_test_data``, ``_cleanup_test_data``) with /api prefix fallback.
- :mod:`browser` — Playwright actions: navigation steps, Flutter login flow,
  computed-style extraction.
- :mod:`runner` — capture orchestration, post-capture dedup, public ``run``.

All public symbols are re-exported here for backward compatibility — callers
that import from ``figma_audit.phases.capture_app`` keep working unchanged.
"""

from __future__ import annotations

# Public API
from figma_audit.phases.capture_app.api_client import (
    _api_login,
    _api_request_with_prefix_fallback,
    _cleanup_test_data,
    _endpoint_variants,
    _extract_jwt_sub,
    _pre_auth_accounts,
    _run_setup_dag,
    _setup_test_data,
)
from figma_audit.phases.capture_app.browser import (
    _execute_navigation_step,
    _extract_computed_styles,
    _flutter_login,
)
from figma_audit.phases.capture_app.runner import (
    _capture_route,
    _dedupe_captures,
    _run_async,
    run,
)
from figma_audit.phases.capture_app.templates import (
    _PLACEHOLDER_MARKERS,
    UnresolvedPlaceholderError,
    _assert_url_resolved,
    _extract_path,
    _resolve_now_token,
    _resolve_payload,
    _resolve_template,
    _slugify,
)

__all__ = [
    "UnresolvedPlaceholderError",
    "run",
    # Re-exported helpers (used by tests, setup_test_data, and callers)
    "_PLACEHOLDER_MARKERS",
    "_api_login",
    "_api_request_with_prefix_fallback",
    "_assert_url_resolved",
    "_capture_route",
    "_cleanup_test_data",
    "_dedupe_captures",
    "_endpoint_variants",
    "_execute_navigation_step",
    "_extract_computed_styles",
    "_extract_jwt_sub",
    "_extract_path",
    "_flutter_login",
    "_pre_auth_accounts",
    "_resolve_now_token",
    "_resolve_payload",
    "_resolve_template",
    "_run_async",
    "_run_setup_dag",
    "_setup_test_data",
    "_slugify",
]
