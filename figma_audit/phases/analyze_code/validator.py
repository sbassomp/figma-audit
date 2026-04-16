"""Phase 1 sanity-check pass on the generated pages_manifest.

The AI producing the manifest sometimes emits patterns that look plausible
but blow up at capture time (runs #47–#48 are the canonical examples).
Running these checks right after Phase 1 gives the user a clear report
with the offending page id *before* Phase 4 navigates to a literal
``/:identifier`` URL or hits a redirect loop on a mis-flagged public page.

Scope is deliberately narrow: only checks whose signal is unambiguous
from the manifest itself (no network, no code inspection). Anything that
could be a false positive stays at capture time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from rich.console import Console

Severity = Literal["error", "warning", "fixed"]


@dataclass
class ValidationIssue:
    severity: Severity
    page_id: str | None
    code: str
    message: str


_ROUTE_PARAM_RE = re.compile(r"/:[A-Za-z_][A-Za-z0-9_]*(?:/|$|\?)")
_TEMPLATE_RE = re.compile(r"\$\{([^}]+)\}")

# Steps whose ``url``/``pattern`` target a real route the runner will
# navigate to (and therefore must be free of literal ``:id`` markers).
_URL_BEARING_ACTIONS = {"navigate", "bridge_push", "wait_for_url"}


def validate_manifest(manifest: dict) -> tuple[dict, list[ValidationIssue]]:
    """Run checks over a Phase 1 manifest and optionally auto-fix it.

    Returns a (manifest, issues) tuple. The manifest is mutated in place
    when an auto-fix applies — callers that need the original should
    deep-copy before calling.

    Checks performed:

    1. **Literal route parameters** in navigation URLs. A step like
       ``{"action": "navigate", "url": "/orders/:id"}`` means the agent
       forgot to substitute the real id via a ``${...}`` template. Emits
       an ``error``; the capture would fail loudly anyway, but Phase 1
       is a much better place to surface the bug.
    2. **Unknown ``${X_user_id}`` templates**. The runner auto-populates
       ``user_id``, ``default_viewer_user_id`` and one ``<role>_user_id``
       per declared account. Any other ``${*_user_id}`` template is a
       hallucination. Emits ``error`` + lists the valid aliases so the
       user can rename.
    3. **auth_required / reach_paths consistency**. When every
       reach_path declares ``required_auth: authenticated`` but the page
       is marked ``auth_required: false``, auto-fix to ``true``. The
       Phase 4 runner picks reach_paths over navigation_steps, so the
       stale flag would mislead the capture (wrong browser state, fail).
    4. **Duplicate ids**. Duplicate ``page.id`` across the manifest or
       duplicate ``state_id`` inside a single page. Each must be unique.
    """
    issues: list[ValidationIssue] = []
    pages = manifest.get("pages") or []
    test_setup = manifest.get("test_setup") or {}
    accounts = test_setup.get("accounts") or {}
    role_names = set(accounts.keys())
    known_user_id_aliases = {"user_id", "default_viewer_user_id"} | {
        f"{r}_user_id" for r in role_names
    }

    page_id_counts: dict[str, int] = {}
    for idx, page in enumerate(pages):
        pid = page.get("id") or f"<page #{idx}>"
        page_id_counts[pid] = page_id_counts.get(pid, 0) + 1

        _check_literal_route_params(page, pid, issues)
        _check_user_id_templates(page, pid, known_user_id_aliases, issues)
        _maybe_fix_auth_required(page, pid, issues)
        _check_duplicate_state_ids(page, pid, issues)

    for pid, n in page_id_counts.items():
        if n > 1:
            issues.append(
                ValidationIssue(
                    severity="error",
                    page_id=pid,
                    code="duplicate_page_id",
                    message=(
                        f"Duplicate page id '{pid}' ({n} occurrences). "
                        "Each page id must be unique — rename in the manifest."
                    ),
                )
            )

    return manifest, issues


def _iter_url_steps(page: dict):
    """Yield every URL-bearing step in ``navigation_steps`` and
    ``reach_paths[*].steps`` as (step_dict, source_label) pairs."""
    for step in page.get("navigation_steps") or []:
        if isinstance(step, dict):
            yield step, "navigation_steps"
    for rp in page.get("reach_paths") or []:
        name = rp.get("name") or "reach_path"
        for step in rp.get("steps") or []:
            if isinstance(step, dict):
                yield step, f"reach_paths[{name}]"


def _extract_url(step: dict) -> str | None:
    action = step.get("action")
    if action not in _URL_BEARING_ACTIONS:
        return None
    raw = step.get("url") or step.get("pattern")
    return raw if isinstance(raw, str) else None


def _check_literal_route_params(page: dict, pid: str, issues: list) -> None:
    for step, source in _iter_url_steps(page):
        url = _extract_url(step)
        if not url:
            continue
        if _ROUTE_PARAM_RE.search(url):
            issues.append(
                ValidationIssue(
                    severity="error",
                    page_id=pid,
                    code="literal_route_param",
                    message=(
                        f"{source} {step.get('action')} URL '{url}' contains a literal "
                        "route parameter (':id'). Replace with a ${...} template whose "
                        "key is produced by a test_setup.steps[].save entry, or emit a "
                        "bridge_push step that carries the real object via `extra`."
                    ),
                )
            )


def _check_user_id_templates(page: dict, pid: str, known_aliases: set[str], issues: list) -> None:
    seen: set[tuple[str, str]] = set()
    for step, source in _iter_url_steps(page):
        url = _extract_url(step)
        if not url:
            continue
        for m in _TEMPLATE_RE.finditer(url):
            key = m.group(1).strip()
            if key.startswith("test_data."):
                key = key[len("test_data.") :]
            if not key.endswith("_user_id"):
                continue
            if key in known_aliases:
                continue
            sig = (source, key)
            if sig in seen:
                continue
            seen.add(sig)
            issues.append(
                ValidationIssue(
                    severity="error",
                    page_id=pid,
                    code="unknown_user_id_alias",
                    message=(
                        f"{source} URL '{url}' references '${{{key}}}' but '{key}' "
                        f"does not match any account role. Known user_id aliases: "
                        f"{sorted(known_aliases)}. Either rename the template to an "
                        f"existing role (e.g. ${{default_viewer_user_id}}) or declare "
                        f"the matching account in test_setup.accounts."
                    ),
                )
            )


def _maybe_fix_auth_required(page: dict, pid: str, issues: list) -> None:
    reach_paths = page.get("reach_paths") or []
    if not reach_paths:
        return
    if page.get("auth_required") is not False:
        return
    required_auths = {(rp.get("required_auth") or "any").lower() for rp in reach_paths}
    if required_auths and all(a == "authenticated" for a in required_auths):
        page["auth_required"] = True
        issues.append(
            ValidationIssue(
                severity="fixed",
                page_id=pid,
                code="auth_required_mismatch",
                message=(
                    "auth_required was False but every reach_path declares "
                    "required_auth=authenticated — auto-fixed to True so the "
                    "capture runs under a logged-in session."
                ),
            )
        )


def _check_duplicate_state_ids(page: dict, pid: str, issues: list) -> None:
    states = page.get("capturable_states") or []
    counts: dict[str, int] = {}
    for s in states:
        sid = s.get("state_id") if isinstance(s, dict) else None
        if sid:
            counts[sid] = counts.get(sid, 0) + 1
    for sid, n in counts.items():
        if n > 1:
            issues.append(
                ValidationIssue(
                    severity="error",
                    page_id=pid,
                    code="duplicate_state_id",
                    message=(
                        f"Duplicate capturable_states.state_id '{sid}' ({n} occurrences) "
                        "in this page. Each state_id must be unique so Phase 3 can "
                        "match it to a Figma variant."
                    ),
                )
            )


def print_issues(issues: list[ValidationIssue], console: Console | None = None) -> None:
    """Pretty-print validation issues grouped by severity."""
    if not issues:
        return
    console = console or Console()
    by_sev: dict[str, list[ValidationIssue]] = {"error": [], "warning": [], "fixed": []}
    for iss in issues:
        by_sev.setdefault(iss.severity, []).append(iss)

    label = {
        "error": ("[bold red]Manifest validation errors[/bold red]", "red"),
        "warning": ("[bold yellow]Manifest validation warnings[/bold yellow]", "yellow"),
        "fixed": ("[bold cyan]Manifest auto-fixes applied[/bold cyan]", "cyan"),
    }
    for sev in ("error", "warning", "fixed"):
        bucket = by_sev.get(sev) or []
        if not bucket:
            continue
        header, color = label[sev]
        console.print(header)
        for iss in bucket:
            loc = f"[{iss.page_id}] " if iss.page_id else ""
            console.print(f"  [{color}]{iss.code}[/{color}] {loc}{iss.message}")
