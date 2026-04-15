"""Template substitution and placeholder guards used by Phase 4.

This module provides the pure-string helpers that the API client and the
browser navigation layer use to resolve ``${test_data.X}`` and ``${now+1d}``
templates, plus the guard that refuses to navigate to URLs still carrying
unresolved placeholders.
"""

from __future__ import annotations

import re

# Tokens that indicate an unresolved placeholder value. If any of these
# appear in test_data values or in a final URL, the value was never
# replaced by a real seed/login result and the tool should fail loudly
# rather than silently navigate to a nonsense route.
#
# The "test-...", "sample-...", "demo-..." families cover the most common
# Phase 1 hallucinations: when the agent knows a route has a ``:userId``,
# ``:token`` or ``:id`` param but cannot find a seed source, it tends to
# invent values like "test-user-id", "sample-user-id", "demo-token",
# "example-id" that look real enough to slip through. Catching them here
# turns the symptom into a clear capture failure pointing at the missing
# seed step, click-path, or JWT-derived template.
_PLACEHOLDER_MARKERS = (
    "placeholder",
    "todo_",
    "<todo",
    "<replace",
    "xxxxxx",
    "test-token",
    "test-user-id",
    "test-id",
    "test-uuid",
    "sample-user-id",
    "sample-token",
    "sample-id",
    "sample-uuid",
    "demo-user-id",
    "demo-token",
    "demo-id",
    "example-id",
    "example-user-id",
)

# Regex matching the ``now`` magic time token: ``now``, ``now+1d``, ``now-30m``, etc.
_DURATION_PATTERN = re.compile(r"^now(?:([+-])(\d+)([smhd]))?$")


class UnresolvedPlaceholderError(Exception):
    """Raised when a navigation URL still contains placeholder or unresolved
    template values after substitution.

    This guards against the Phase 1 AI emitting test_data entries like
    ``course_id: "placeholder_course_id"`` that silently leak into URLs
    when seed_items fails, producing nonsense requests
    (e.g. ``GET /courses/placeholder_course_id``) that confuse the user
    into thinking the captured page is real.
    """


class NavigationFailedError(Exception):
    """Raised by Phase 4 when a navigation step that was expected to land
    on a specific URL (or whose failure makes the capture meaningless)
    did not succeed.

    Examples that raise this:

    - ``wait_for_url`` timed out: the previous step was supposed to
      change the URL to a known pattern and it did not, so we are still
      on the wrong page.
    - ``navigate`` could not load the requested URL.
    - ``bridge_push`` was requested but the figma-audit JS bridge is not
      installed on the page.

    The runner catches this and marks the capture as a navigation
    failure, refusing to take a screenshot of the wrong page. Best-effort
    steps like ``click`` or ``fill`` do not raise this when they cannot
    find a target — they leave the page where it is and let a downstream
    ``wait_for_url`` decide whether the chain succeeded.
    """


def _resolve_now_token(expr: str) -> str | None:
    """Resolve a ``now`` / ``now+1d`` / ``now-30m`` token to an ISO-8601 UTC timestamp.

    Used inside ``${...}`` placeholders so test_setup payloads can specify
    dates relative to capture time. The Phase 1 AI keeps emitting hard-coded
    dates that go stale (e.g. ``desiredArrivalTime: 2025-01-15``) and get
    rejected by backends that require future timestamps.

    Supported suffixes: ``s`` (seconds), ``m`` (minutes), ``h`` (hours),
    ``d`` (days). Returns ``None`` if the expression is not a now-token.
    """
    from datetime import datetime, timedelta, timezone

    m = _DURATION_PATTERN.match(expr.strip())
    if not m:
        return None
    sign, amount, unit = m.groups()
    delta = timedelta()
    if sign:
        n = int(amount)
        delta = {
            "s": timedelta(seconds=n),
            "m": timedelta(minutes=n),
            "h": timedelta(hours=n),
            "d": timedelta(days=n),
        }[unit]
        if sign == "-":
            delta = -delta
    return (datetime.now(timezone.utc) + delta).strftime("%Y-%m-%dT%H:%M:%SZ")


def _resolve_template(template: str, data: dict) -> str:
    """Resolve ``${key}`` templates in a string.

    Two kinds of placeholders are supported:

    - ``${test_data.key}`` or ``${key}`` — looked up in the ``test_data``
      dict. Returns the original ``${...}`` literal if the key is missing,
      so :func:`_assert_url_resolved` can detect and reject the leftover.
    - ``${now}``, ``${now+1d}``, ``${now-30m}`` — magic time tokens that
      resolve to an ISO-8601 UTC timestamp at substitution time. Lets
      ``test_setup`` payloads always send a future date for fields like
      ``desiredArrivalTime`` without hard-coding values that expire.
    """

    def _replace(m: re.Match) -> str:
        key = m.group(1).strip()
        # Magic now-token
        now_value = _resolve_now_token(key)
        if now_value is not None:
            return now_value
        # test_data lookup
        if key.startswith("test_data."):
            key = key[len("test_data.") :]
        return str(data.get(key, m.group(0)))

    return re.sub(r"\$\{([^}]+)\}", _replace, template)


def _resolve_payload(payload: dict, data: dict) -> dict:
    """Resolve ``${key}`` templates in all string values of a payload dict (recursively)."""
    resolved = {}
    for k, v in payload.items():
        if isinstance(v, str) and "${" in v:
            resolved[k] = _resolve_template(v, data)
        elif isinstance(v, dict):
            resolved[k] = _resolve_payload(v, data)
        else:
            resolved[k] = v
    return resolved


def _extract_path(obj: dict, dotted_path: str) -> str:
    """Extract a value from a nested dict using a dotted path (e.g. ``data.id``)."""
    current = obj
    for part in dotted_path.split("."):
        if isinstance(current, dict):
            current = current.get(part, "")
        else:
            return str(current)
    return str(current)


def _assert_url_resolved(url: str) -> None:
    """Fail loudly if the URL still carries unresolved markers.

    Detects:

    - Leftover ``${...}`` template braces (key not found in test_data)
    - Common placeholder tokens (``placeholder_xxx``, ``todo_xxx``, ``<TODO>``, etc.)

    A matched URL is never navigated to — the caller will mark the capture
    as a navigation failure with a clear error.
    """
    if "${" in url:
        raise UnresolvedPlaceholderError(
            f"URL has unresolved template: {url} "
            "(a ${{...}} key was not found in test_data; check test_setup.seed_items)"
        )
    lower = url.lower()
    for marker in _PLACEHOLDER_MARKERS:
        if marker in lower:
            raise UnresolvedPlaceholderError(
                f"URL contains placeholder marker '{marker}': {url} "
                "(seed_items likely failed — navigation would hit a nonsense route)"
            )


def _slugify(text: str) -> str:
    """Convert text to a filesystem-safe slug for screenshot filenames."""
    s = text.lower().strip()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_]+", "-", s)
    s = re.sub(r"-+", "-", s)
    return s.strip("-") or "page"
