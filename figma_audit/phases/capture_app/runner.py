"""Phase 4 orchestration: capture loop, dedup, and the public ``run`` entry point.

This module ties the browser layer (:mod:`browser`) and the API client
(:mod:`api_client`) together. It also implements the global silent-redirect
detection (:func:`_dedupe_captures`).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import yaml
from playwright.async_api import Page, async_playwright
from rich.console import Console

from figma_audit.config import Config
from figma_audit.phases.capture_app.api_client import (
    _cleanup_test_data,
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
from figma_audit.phases.capture_app.templates import (
    _PLACEHOLDER_MARKERS,
    NavigationFailedError,
    UnresolvedPlaceholderError,
    _assert_url_resolved,
    _resolve_template,
    _slugify,
)

console = Console()


def _select_reach_path(page_info: dict, *, is_authenticated: bool) -> dict | None:
    """Return the most appropriate reach_path for the current browser state.

    Reach paths are the scenario-based output of Phase 1's call-site tracing
    (see docs). Each page may carry a ``reach_paths`` list ordered from most
    preferred to least. Each path has:

    - ``name`` (identifier, used in logs)
    - ``required_auth`` — ``"guest"`` / ``"authenticated"`` / ``"any"``
    - ``steps`` — a list of navigation primitives executed via
      :func:`_execute_navigation_step`
    - optional ``description`` — why this path exists, extracted from the
      widget code that contains the matching ``push`` call site

    Selection rule:

    1. Skip paths whose ``required_auth`` is incompatible with the current
       browser session (guest path when logged in, or vice versa).
    2. Among compatible paths, return the first one (the agent lists them
       in preference order).
    3. If nothing fits, return ``None`` and let the caller fall back to the
       legacy ``navigation_steps`` entry.
    """
    paths = page_info.get("reach_paths") or []
    if not paths:
        return None

    for path in paths:
        required = (path.get("required_auth") or "any").lower()
        if required == "guest" and is_authenticated:
            continue
        if required == "authenticated" and not is_authenticated:
            continue
        return path
    return None


async def _capture_route(
    page: Page,
    page_info: dict,
    app_url: str,
    test_data: dict,
    screenshots_dir: Path,
    *,
    is_authenticated: bool = True,
) -> tuple[dict, list[dict] | None]:
    """Capture a single route: navigate, screenshot, extract styles."""
    page_id = page_info["id"]
    route = page_info["route"]
    slug = _slugify(page_id)

    console.print(f"  Capturing {page_id} ({route})...")

    # Navigate
    placeholder_error: str | None = None
    nav_failure: str | None = None
    # Prefer a reach_path (scenario-based) when Phase 1 emitted one, because
    # those were derived by tracing the actual call sites in the widget code
    # and are therefore more reliable than a bare URL guess. Fall back to the
    # legacy ``navigation_steps`` for pages that predate the reach_path
    # schema or for which the agent could not synthesise a scenario.
    selected_path = _select_reach_path(page_info, is_authenticated=is_authenticated)
    if selected_path is not None:
        nav_steps = selected_path.get("steps") or []
        console.print(
            f"    [dim]reach_path: {selected_path.get('name', '?')} "
            f"(auth={selected_path.get('required_auth', 'any')})[/dim]"
        )
    else:
        nav_steps = page_info.get("navigation_steps", [])
    if nav_steps:
        for step in nav_steps:
            try:
                await _execute_navigation_step(page, step, test_data)
            except UnresolvedPlaceholderError as e:
                # Unresolved placeholder is a hard failure for this capture —
                # we must not pretend it succeeded. Abort the nav sequence
                # and mark the capture as errored with the specific reason.
                placeholder_error = str(e)
                console.print(f"    [red]{placeholder_error}[/red]")
                break
            except NavigationFailedError as e:
                # A structurally critical step (navigate, bridge_push,
                # wait_for_url) failed. Whatever the browser is showing
                # right now is the wrong page, so we must NOT screenshot
                # it and pretend the capture is OK. Set nav_failure and
                # short-circuit below.
                nav_failure = str(e)
                console.print(f"    [red]Navigation failed: {nav_failure}[/red]")
                break
            except Exception as e:
                # Best-effort steps (click, fill, wait) are allowed to fail
                # without invalidating the capture; their failure typically
                # cascades into a wait_for_url that does raise hard if the
                # chain is broken.
                console.print(f"    [yellow]Nav step failed: {step.get('action')} -- {e}[/yellow]")
    else:
        # Simple direct navigation
        url = app_url.rstrip("/") + route
        try:
            _assert_url_resolved(url)
            await page.goto(url, wait_until="networkidle", timeout=15000)
        except UnresolvedPlaceholderError as e:
            placeholder_error = str(e)
            console.print(f"    [red]{placeholder_error}[/red]")
        except Exception as e:
            console.print(f"    [yellow]Navigation failed: {e}[/yellow]")

    if placeholder_error:
        # Short-circuit: return a failed capture so Phase 5 skips it and the
        # run page surfaces the reason. No screenshot is taken.
        return (
            {
                "page_id": page_id,
                "route": route,
                "landed_url": page.url,
                "screenshot": None,
                "error": f"Unresolved placeholder: {placeholder_error}",
            },
            None,
        )

    if nav_failure:
        return (
            {
                "page_id": page_id,
                "route": route,
                "landed_url": page.url,
                "screenshot": None,
                "error": f"Navigation failed: {nav_failure}",
            },
            None,
        )

    # Wait a bit for rendering to settle
    await page.wait_for_timeout(1000)

    # Screenshot
    screenshot_path = screenshots_dir / f"{slug}.png"
    await page.screenshot(path=str(screenshot_path), full_page=False)

    # Capture the final landed URL. When the route has path params, this
    # shows the substituted value (e.g. /items/42 instead of /items/:id).
    # When the app redirected us (auth guard, 404 fallback), this surfaces
    # the actual destination so the user can diagnose silent redirects.
    final_url = page.url

    # Note: silent-redirect detection is handled globally after all captures
    # finish, via _dedupe_captures() which hashes every screenshot (including
    # wizard states) and flags any hash shared by 2+ captures as a navigation
    # failure. Doing it post-capture avoids the order-dependency of comparing
    # against a single reference image that may not exist yet.

    # Extract styles
    styles = await _extract_computed_styles(page)

    result: dict = {
        "page_id": page_id,
        "route": route,
        "landed_url": final_url,
        "screenshot": f"app_screenshots/{slug}.png",
        "styles_available": styles is not None,
    }

    # Capture additional states (wizard steps, tabs, filters) if defined.
    #
    # A capturable_state entry supports two navigation styles:
    #
    # - ``delta_steps``: list of click/fill/wait primitives applied from
    #   the CURRENT page to reach the state. Wizard-friendly, order matters.
    # - ``query``: dict of query parameters to merge into the CURRENT URL
    #   (preserving path params, path segments and auth state). Tab and
    #   filter friendly: a fresh navigation lands on the requested state
    #   without having to simulate clicks through the UI.
    #
    # The first state is always the one already captured by the code
    # above, so it reuses the existing screenshot without re-navigating.
    capturable_states = page_info.get("capturable_states", [])
    if capturable_states:
        state_screenshots = []

        first = capturable_states[0]
        state_screenshots.append(
            {
                "state_id": first["state_id"],
                "screenshot": f"app_screenshots/{slug}.png",
            }
        )

        for state_idx, state in enumerate(capturable_states[1:], start=2):
            state_id = state["state_id"]
            delta_steps = state.get("delta_steps") or []
            query = state.get("query") or {}
            state_slug = f"{slug}--{_slugify(state_id)}"
            state_screenshot_rel = f"app_screenshots/{state_slug}.png"
            state_screenshot_path = screenshots_dir / f"{state_slug}.png"

            console.print(f"    State {state_idx}/{len(capturable_states)}: {state_id}...")

            try:
                if query:
                    # Merge query params into the current URL, preserving
                    # path (including substituted route params) and host.
                    from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

                    parsed = urlparse(page.url)
                    merged_qs = dict(parse_qsl(parsed.query, keep_blank_values=False))
                    for k, v in query.items():
                        val = str(v)
                        if "${" in val:
                            val = _resolve_template(val, test_data)
                        if val == "" or val is None:
                            merged_qs.pop(k, None)
                        else:
                            merged_qs[k] = val
                    new_url = urlunparse(parsed._replace(query=urlencode(merged_qs, doseq=False)))
                    _assert_url_resolved(new_url)
                    await page.goto(new_url, wait_until="networkidle", timeout=15000)
                elif delta_steps:
                    for step in delta_steps:
                        await _execute_navigation_step(page, step, test_data)
                # If neither: we stay on the current page and screenshot again
                # (useful for dark-mode variants triggered by page.emulateMedia
                # which is set at context level, but also as an escape hatch).
                await page.wait_for_timeout(1000)
                await page.screenshot(path=str(state_screenshot_path), full_page=False)
                state_screenshots.append(
                    {
                        "state_id": state_id,
                        "landed_url": page.url,
                        "screenshot": state_screenshot_rel,
                    }
                )
            except UnresolvedPlaceholderError as e:
                console.print(f"    [red]State {state_id} unresolved: {e}[/red]")
                state_screenshots.append(
                    {
                        "state_id": state_id,
                        "landed_url": page.url,
                        "screenshot": None,
                        "error": f"Unresolved placeholder: {e}",
                    }
                )
            except Exception as e:
                console.print(f"    [yellow]State {state_id} failed: {e}[/yellow]")
                state_screenshots.append(
                    {
                        "state_id": state_id,
                        "landed_url": page.url,
                        "screenshot": None,
                        "error": str(e)[:200],
                    }
                )

        result["states"] = state_screenshots

    return result, styles


def _dedupe_captures(all_results: list[dict], screenshots_dir: Path) -> tuple[int, int]:
    """Global silent-redirect detection.

    Hashes every screenshot file (top-level captures + wizard states) and
    flags silent navigation failures as follows:

    For each unique hash that appears in 2+ locations, we keep the capture
    whose ROUTE is shortest (and which is a top-level capture, not a wizard
    state) and mark every other occurrence as a navigation failure. The
    rationale: when multiple pages produce identical screenshots, the app
    silently redirected the deeper/more-specific routes to a shallower
    landing page (home, list, account menu, splash). The shortest route is
    the most plausible "legitimate" owner of that landing screen.

    Mutates ``all_results`` in place. Failed captures get
    ``screenshot=None`` and ``error="Duplicate of <kept_page_id>'s
    screenshot — navigation likely failed"``. Wizard states that fall
    through to the same image as a sibling state (or another page) are
    flagged similarly.

    Returns ``(failed_captures, failed_states)`` counts.
    """
    import hashlib

    # Walk all captures, collecting (result_idx, state_idx_or_None, rel_path,
    # route_len, is_top_level) for every screenshot.
    #
    # The first capturable state (index 0) is by convention the base capture
    # itself (same screenshot path). Skipping it here prevents the dedup from
    # flagging it as a "duplicate of itself" and setting its screenshot to
    # None, which would break the compare phase fallback.
    locations: list[tuple[int, int | None, str, int, bool]] = []
    for i, result in enumerate(all_results):
        route = result.get("route") or ""
        route_len = len(route)
        top_path = result.get("screenshot")
        if top_path:
            locations.append((i, None, top_path, route_len, True))
        for s_idx, state in enumerate(result.get("states", []) or []):
            if s_idx == 0:
                # First state always reuses the base screenshot path; not a
                # dedup candidate.
                continue
            sp = state.get("screenshot")
            if sp:
                # Wizard states get a length penalty so they never beat their
                # parent route on equal length.
                locations.append((i, s_idx, sp, route_len + 100, False))

    # Hash every screenshot file once (cache by path).
    path_hash: dict[str, str] = {}
    for _, _, rel_path, _, _ in locations:
        if rel_path in path_hash:
            continue
        abs_path = screenshots_dir.parent / rel_path
        if not abs_path.exists():
            continue
        try:
            path_hash[rel_path] = hashlib.md5(abs_path.read_bytes()).hexdigest()
        except OSError:
            continue

    # Group locations by hash; for each hash, the location with the shortest
    # route is the "winner" and everything else is flagged.
    hash_to_locs: dict[str, list[tuple[int, int | None, str, int, bool]]] = {}
    for loc in locations:
        h = path_hash.get(loc[2])
        if h is None:
            continue
        hash_to_locs.setdefault(h, []).append(loc)

    failed_captures = 0
    failed_states = 0
    for h, locs in hash_to_locs.items():
        if len(locs) < 2:
            continue
        # Winner: shortest route, top-level beats state on tie (already
        # encoded in the +100 penalty applied to states).
        winner = min(locs, key=lambda loc: (loc[3], loc[2]))
        winner_page_id = all_results[winner[0]].get("page_id", "?")

        for loc in locs:
            if loc is winner:
                continue
            result_idx, state_idx, _rel_path, _, _ = loc
            result = all_results[result_idx]
            err_msg = (
                f"Duplicate screenshot of '{winner_page_id}' — "
                "navigation likely failed (silent redirect)"
            )
            if state_idx is None:
                result["screenshot"] = None
                result["error"] = err_msg
                result["duplicate_hash"] = h
                failed_captures += 1
            else:
                state = result["states"][state_idx]
                state["screenshot"] = None
                state["error"] = err_msg
                failed_states += 1

    return failed_captures, failed_states


async def _run_async(config: Config) -> Path:
    """Async implementation of Phase 4."""
    output_dir = config.output_dir
    mapping_path = output_dir / "screen_mapping.yaml"
    pages_manifest_path = output_dir / "pages_manifest.json"
    screenshots_dir = output_dir / "app_screenshots"
    styles_path = output_dir / "app_styles.json"

    if not mapping_path.exists():
        raise FileNotFoundError("screen_mapping.yaml not found. Run Phase 3 first.")
    if not pages_manifest_path.exists():
        raise FileNotFoundError("pages_manifest.json not found. Run Phase 1 first.")

    with open(mapping_path) as f:
        mapping_data = yaml.safe_load(f)
    with open(pages_manifest_path) as f:
        pages_manifest = json.load(f)

    if not mapping_data.get("verified"):
        raise ValueError(
            "screen_mapping.yaml has verified: false. "
            "Review the mapping and set verified: true to continue."
        )

    app_url = config.app_url
    if not app_url:
        raise ValueError("No app URL. Provide --app-url.")

    # Build lookup: page_id -> page info
    pages_by_id = {p["id"]: p for p in pages_manifest.get("pages", [])}
    test_data = pages_manifest.get("test_data", {})
    renderer = pages_manifest.get("renderer", "dom")

    # Get unique page_ids from mapping (deduplicate)
    mapped_page_ids = set()
    for m in mapping_data.get("mappings", []):
        pid = m.get("page_id")
        if pid and m.get("route"):
            mapped_page_ids.add(pid)

    pages_to_capture = [pages_by_id[pid] for pid in mapped_page_ids if pid in pages_by_id]
    console.print(f"[bold]Capturing {len(pages_to_capture)} unique pages from {app_url}[/bold]")
    console.print(f"  Renderer: {renderer}")

    if renderer == "canvaskit":
        console.print("  [yellow]CanvasKit detected -- DOM styles extraction disabled[/yellow]")

    screenshots_dir.mkdir(parents=True, exist_ok=True)

    # Override test_data credentials with config if available
    if config.test_credentials.email:
        test_data["email"] = config.test_credentials.email
        test_data["otp"] = config.test_credentials.otp

    # Populate app with test data via API calls.
    # Priority: config YAML test_setup > manifest test_setup (AI-generated, less reliable).
    test_setup = config.test_setup or pages_manifest.get("test_setup", {})
    parsed_setup = config.test_setup_model()

    created_item_ids: list[str] = []
    taken_item_id: str | None = None
    viewer_role: str | None = None

    # Multi-actor path: the new-shape test_setup explicitly declares accounts
    # and a DAG of seed steps. Pre-auth every account, run the DAG via API
    # (without touching the browser), then pick the default_viewer's
    # credentials so the browser login logs in as that role.
    use_multi_actor = bool(parsed_setup.steps) or len(parsed_setup.accounts) >= 2
    if use_multi_actor and parsed_setup.accounts:
        console.print("\n[bold]Multi-actor test_setup detected[/bold]")
        console.print(f"  Accounts: {', '.join(parsed_setup.accounts.keys())}")
        console.print(f"  Steps: {len(parsed_setup.steps)}")
        console.print(f"  Default viewer: {parsed_setup.default_viewer or '(first)'}")

        # api_prefix_hint is mutated into this dict on the first successful
        # login and reused across every subsequent login + seed step call.
        login_dict = dict(test_setup or {})
        tokens = _pre_auth_accounts(app_url, login_dict, parsed_setup.accounts)

        # Expose each authenticated account's stable user id as
        # ``<role>_user_id`` in test_data. Routes like /profile/:userId or
        # /users/:id can then template ``${driver_user_id}`` instead of
        # leaking a hallucinated placeholder. Runs before the DAG so a
        # seed step that references the current user works too.
        for role, token in tokens.items():
            sub = _extract_jwt_sub(token)
            if sub:
                test_data[f"{role}_user_id"] = sub

        if tokens and parsed_setup.steps:
            _run_setup_dag(app_url, parsed_setup, tokens, test_data, login_dict)

        # Select the viewer account whose credentials will drive the browser
        # login. Preference: declared default_viewer → first logged-in account.
        viewer_role = (
            parsed_setup.default_viewer
            if parsed_setup.default_viewer and parsed_setup.default_viewer in tokens
            else (next(iter(tokens), None))
        )
        if viewer_role and viewer_role in parsed_setup.accounts:
            viewer_acct = parsed_setup.accounts[viewer_role]
            if viewer_acct.email:
                test_data["email"] = viewer_acct.email
                test_data["otp"] = viewer_acct.otp
                console.print(
                    f"  [dim]Browser will log in as '{viewer_role}' ({viewer_acct.email})[/dim]"
                )
    else:
        # Legacy mono-actor path: seed_items + optional take_item. Kept for
        # backward compatibility with configs that haven't been migrated.
        seed_account = config.seed_account.model_dump() if config.seed_account.email else None
        if test_setup.get("seed_items") and (seed_account or test_data.get("email")) and app_url:
            created_item_ids, taken_item_id = _setup_test_data(
                app_url, test_data, test_setup, seed_account=seed_account
            )
        # Note: _setup_test_data injects IDs directly into test_data
        # using the test_data_key from each seed_item spec in the manifest

    # Purge any placeholder values still lingering in test_data. If seed_items
    # failed (bad payload, auth issue, etc.) the AI-generated placeholder
    # strings like "placeholder_course_id" would otherwise leak into every
    # templated URL, producing silent nonsense navigation. Removing them
    # forces _assert_url_resolved to fail loudly on each affected capture.
    leaked_placeholders = []
    for k, v in list(test_data.items()):
        if isinstance(v, str) and any(marker in v.lower() for marker in _PLACEHOLDER_MARKERS):
            leaked_placeholders.append((k, v))
            del test_data[k]
    if leaked_placeholders:
        console.print(
            f"  [yellow]Warning: {len(leaked_placeholders)} unresolved "
            "placeholder(s) in test_data — captures needing these values "
            "will fail with a clear error:[/yellow]"
        )
        for k, v in leaked_placeholders:
            console.print(f"    [yellow]  {k} = {v!r}[/yellow]")

    # Launch browser
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        # Flutter CanvasKit uses devicePixelRatio to scale content.
        # Use scale_factor=1 to match logical pixels, avoiding zoomed rendering.
        context = await browser.new_context(
            viewport={
                "width": config.viewport.width,
                "height": config.viewport.height,
            },
            device_scale_factor=1,
        )
        page = await context.new_page()

        # Split pages: capture public pages BEFORE login, auth-required pages AFTER.
        # This ensures splash/welcome screens are captured in the logged-out state,
        # before the app redirects authenticated users away from them.
        public_pages = [p for p in pages_to_capture if not p.get("auth_required")]
        auth_pages = [p for p in pages_to_capture if p.get("auth_required")]

        all_results: list[dict] = []
        all_styles: dict = {}

        from figma_audit.utils.progress import get_progress

        run_progress = get_progress()
        total_pages = len(pages_to_capture)
        cap_idx = 0

        async def _capture_batch(
            batch: list[dict],
            active_viewer: str | None,
            *,
            is_authenticated: bool,
        ) -> None:
            nonlocal cap_idx
            for page_info in batch:
                if run_progress:
                    run_progress.update(
                        step=f"{page_info['id']} ({page_info['route']})",
                        progress=cap_idx + 1,
                        total=total_pages,
                    )
                # Per-page viewer override: a page may declare its own viewer
                # role. When it matches the active browser session we tag the
                # capture; when it differs we still tag but log a warning so
                # the user knows the capture may be wrong.
                page_viewer = page_info.get("viewer") or active_viewer
                if (
                    page_info.get("viewer")
                    and active_viewer
                    and page_info["viewer"] != active_viewer
                ):
                    console.print(
                        f"    [yellow]Page '{page_info['id']}' wants viewer "
                        f"'{page_info['viewer']}' but browser is logged in as "
                        f"'{active_viewer}' — capture may be incorrect[/yellow]"
                    )
                try:
                    result, styles = await _capture_route(
                        page,
                        page_info,
                        app_url,
                        test_data,
                        screenshots_dir,
                        is_authenticated=is_authenticated,
                    )
                    if page_viewer:
                        result["viewer_role"] = page_viewer
                    all_results.append(result)
                    if styles:
                        all_styles[page_info["id"]] = styles
                except Exception as e:
                    console.print(f"  [red]Error capturing {page_info['id']}: {e}[/red]")
                    failure: dict = {
                        "page_id": page_info["id"],
                        "route": page_info["route"],
                        "screenshot": None,
                        "error": str(e),
                    }
                    if page_viewer:
                        failure["viewer_role"] = page_viewer
                    all_results.append(failure)
                cap_idx += 1

        # ── Phase A: Capture public pages (before login) ─────────────
        if public_pages:
            console.print(
                f"\n  [bold]Capturing {len(public_pages)} public page(s) (before login)...[/bold]"
            )
            console.print(f"  Loading {app_url}...")
            try:
                await page.goto(app_url, wait_until="networkidle", timeout=30000)
            except Exception as e:
                console.print(f"  [yellow]Initial load: {e}[/yellow]")

            # Public pages have no logged-in viewer.
            await _capture_batch(public_pages, active_viewer=None, is_authenticated=False)

        # ── Phase B: Authenticate ─────────────────────────────────────
        auth_email = test_data.get("email") or test_data.get("phone")
        auth_otp = test_data.get("otp", "1234")
        if auth_email:
            # Reload app to start fresh for login (public captures may have navigated away)
            try:
                await page.goto(app_url, wait_until="networkidle", timeout=30000)
            except Exception as e:
                console.print(f"  [yellow]Pre-login load: {e}[/yellow]")

            logged_in = await _flutter_login(page, app_url, auth_email, auth_otp)
            if logged_in:
                label = f" as '{viewer_role}'" if viewer_role else ""
                console.print(f"  [green]Authentication successful{label}[/green]")
            else:
                console.print(
                    "  [yellow]Authentication failed -- continuing without login[/yellow]"
                )

        # ── Phase C: Capture auth-required pages (after login) ────────
        if auth_pages:
            console.print(f"\n  [bold]Capturing {len(auth_pages)} authenticated page(s)...[/bold]")
            await _capture_batch(auth_pages, active_viewer=viewer_role, is_authenticated=True)

        await browser.close()

    # Cleanup test data
    if created_item_ids:
        cleanup_creds = seed_account if seed_account else test_data
        _cleanup_test_data(app_url, cleanup_creds, test_setup, created_item_ids)
    _ = taken_item_id  # tracked for future use

    # Global post-capture dedup: detect silent redirects by hashing every
    # screenshot and flagging any hash shared by 2+ captures.
    failed_captures, failed_states = _dedupe_captures(all_results, screenshots_dir)

    # Save results
    captures_path = output_dir / "app_captures.json"
    with open(captures_path, "w") as f:
        json.dump(all_results, f, indent=2, ensure_ascii=False)

    if all_styles:
        with open(styles_path, "w") as f:
            json.dump(all_styles, f, indent=2, ensure_ascii=False)

    captured = sum(1 for r in all_results if r.get("screenshot"))
    errors = sum(1 for r in all_results if r.get("error"))

    console.print("\n[bold green]Capture done.[/bold green]")
    console.print(f"  {captured}/{len(all_results)} screenshots saved to {screenshots_dir}")
    if all_styles:
        console.print(f"  {len(all_styles)} pages with DOM styles extracted")
    if failed_captures or failed_states:
        console.print(
            f"  [yellow]{failed_captures} page(s) and {failed_states} wizard state(s) "
            f"flagged as silent redirects (duplicate screenshots)[/yellow]"
        )
    if errors:
        console.print(f"  [yellow]{errors} pages with errors[/yellow]")

    return captures_path


def run(config: Config) -> Path:
    """Run Phase 4: Capture app screenshots via Playwright."""
    return asyncio.run(_run_async(config))
