"""Phase 4: Capture App -- Navigate deployed app with Playwright, take screenshots."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import yaml
from playwright.async_api import Page, async_playwright
from rich.console import Console

from figma_audit.config import Config

console = Console()


async def _execute_navigation_step(page: Page, step: dict, test_data: dict) -> None:
    """Execute a single navigation step from the manifest."""
    action = step.get("action", "")
    timeout = step.get("timeout", 10000)

    if action == "navigate":
        url = step.get("url", "")
        # Resolve ${test_data.key} templates in URLs
        if "${" in url:
            url = _resolve_template(url, test_data)
        if url.startswith("/"):
            url = page.url.split("/")[0] + "//" + page.url.split("/")[2] + url
        await page.goto(url, wait_until="networkidle", timeout=timeout)

    elif action == "click":
        selector = step.get("selector", "")
        text = step.get("text", "")
        x, y = step.get("x"), step.get("y")
        clicked = False

        # Strategy 1: CSS selector (works for DOM-based apps)
        if selector and not clicked:
            try:
                await page.click(selector, timeout=min(timeout, 3000))
                clicked = True
            except Exception:
                pass

        # Strategy 2: Accessibility role (works for Flutter CanvasKit with Semantics)
        if text and not clicked:
            for role in ("button", "link", "tab", "menuitem"):
                try:
                    await page.get_by_role(role, name=text).click(timeout=min(timeout, 3000))
                    clicked = True
                    break
                except Exception:
                    pass

        # Strategy 3: Text-based (works for HTML renderer)
        if text and not clicked:
            try:
                await page.get_by_text(text, exact=False).first.click(timeout=min(timeout, 3000))
                clicked = True
            except Exception:
                pass

        # Strategy 4: Coordinate-based (last resort)
        if x is not None and y is not None and not clicked:
            try:
                await page.mouse.click(x, y)
                clicked = True
            except Exception:
                pass

        if not clicked:
            console.print(
                f"    [dim]Click failed: selector={selector} text={text} x={x} y={y}[/dim]"
            )

    elif action == "fill":
        selector = step.get("selector", "")
        label = step.get("label", "")
        value = step.get("value", "")
        # Resolve ${test_data.key} templates
        if "${" in value:
            value = _resolve_template(value, test_data)
        filled = False

        # Strategy 1: CSS selector
        if selector and not filled:
            try:
                await page.fill(selector, value, timeout=min(timeout, 3000))
                filled = True
            except Exception:
                pass

        # Strategy 2: Accessibility label (works for Flutter CanvasKit)
        if label and not filled:
            try:
                await page.get_by_label(label).fill(value, timeout=min(timeout, 3000))
                filled = True
            except Exception:
                pass

        # Strategy 3: Placeholder text
        placeholder = step.get("placeholder", "")
        if placeholder and not filled:
            try:
                await page.get_by_placeholder(placeholder).fill(value, timeout=min(timeout, 3000))
                filled = True
            except Exception:
                pass

        if not filled:
            console.print(f"    [dim]Fill failed: selector={selector} label={label}[/dim]")

    elif action == "wait":
        selector = step.get("selector")
        if selector:
            await page.wait_for_selector(selector, timeout=timeout)
        else:
            await page.wait_for_timeout(timeout)

    elif action == "wait_for_url":
        pattern = step.get("pattern", "")
        await page.wait_for_url(pattern, timeout=timeout)

    elif action == "screenshot":
        pass  # Handled by the caller


def _resolve_template(template: str, data: dict) -> str:
    """Resolve ${key} templates in a string."""
    import re as _re

    def _replace(m: _re.Match) -> str:
        key = m.group(1)
        if key.startswith("test_data."):
            key = key[len("test_data."):]
        return str(data.get(key, m.group(0)))

    return _re.sub(r"\$\{([^}]+)\}", _replace, template)


def _resolve_payload(payload: dict, data: dict) -> dict:
    """Resolve ${key} templates in all string values of a payload dict."""
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
    """Extract a value from a nested dict using a dotted path (e.g. 'data.id')."""
    current = obj
    for part in dotted_path.split("."):
        if isinstance(current, dict):
            current = current.get(part, "")
        else:
            return str(current)
    return str(current)


def _api_login(
    base_url: str, test_setup: dict, credentials: dict
) -> str | None:
    """Authenticate via the app API using manifest config. Returns bearer token."""
    import requests

    auth_endpoint = test_setup.get("auth_endpoint", "")
    if not auth_endpoint:
        return None

    payload = _resolve_payload(test_setup.get("auth_payload", {}), credentials)

    try:
        otp_endpoint = test_setup.get("auth_otp_request_endpoint")
        if otp_endpoint:
            requests.post(
                f"{base_url}{otp_endpoint}",
                json={"email": credentials.get("email")},
                timeout=10,
            )
        resp = requests.post(f"{base_url}{auth_endpoint}", json=payload, timeout=10)
        if resp.status_code != 200:
            console.print(
                f"    [yellow]API login failed ({resp.status_code}): "
                f"{resp.text[:100]}[/yellow]"
            )
            return None
        token_path = test_setup.get("auth_token_path", "accessToken")
        token = _extract_path(resp.json(), token_path)
        if not token:
            console.print(f"    [yellow]No token at '{token_path}'[/yellow]")
            return None
        return token
    except Exception as e:
        console.print(f"    [yellow]API login error: {e}[/yellow]")
        return None


def _setup_test_data(
    app_url: str,
    test_data: dict,
    test_setup: dict,
    seed_account: dict | None = None,
) -> tuple[list[str], str | None]:
    """Create test data via manifest-driven API calls.

    Reads test_setup from the manifest to know which endpoints to call.
    Returns (created_item_ids, taken_item_id).
    """
    import requests

    base = app_url.rstrip("/")

    if not test_setup or not test_setup.get("seed_items"):
        return [], None

    console.print("  [bold]Setting up test data via API...[/bold]")

    # Login with seed account (items created by seed appear as "available" for main user)
    seed_creds = seed_account or test_data
    token = _api_login(base, test_setup, seed_creds)
    if not token:
        return [], None
    console.print("    API login OK (seed)")
    headers = {"Authorization": f"Bearer {token}"}

    # Create seed items from manifest config
    created_ids: list[str] = []
    for i, item_spec in enumerate(test_setup["seed_items"]):
        endpoint = _resolve_template(item_spec.get("endpoint", ""), test_data)
        method = item_spec.get("method", "POST").upper()
        payload = _resolve_payload(item_spec.get("payload", {}), test_data)
        id_path = item_spec.get("id_path", "id")
        td_key = item_spec.get("test_data_key", f"item_{i}")

        try:
            resp = requests.request(
                method, f"{base}{endpoint}", json=payload, headers=headers, timeout=10
            )
            if resp.status_code in (200, 201):
                item_id = _extract_path(resp.json(), id_path)
                if item_id:
                    created_ids.append(item_id)
                    test_data[td_key] = item_id
                console.print(f"    Item {i + 1} created ({td_key}={item_id})")
            else:
                console.print(
                    f"    [yellow]Item {i + 1} failed ({resp.status_code}): "
                    f"{resp.text[:100]}[/yellow]"
                )
        except Exception as e:
            console.print(f"    [yellow]Item {i + 1} error: {e}[/yellow]")

    console.print(f"  [green]{len(created_ids)} test item(s) created[/green]")

    # Take the first item with the MAIN user (if configured in manifest)
    taken_id = None
    take_spec = test_setup.get("take_item")
    if take_spec and created_ids:
        main_email = test_data.get("email")
        seed_email = (seed_account or {}).get("email")
        if main_email and main_email != seed_email:
            main_token = _api_login(base, test_setup, test_data)
            if main_token:
                cid = created_ids[0]
                merged = {**test_data, "item_id": cid}
                endpoint = _resolve_template(take_spec.get("endpoint", ""), merged)
                td_key = take_spec.get("test_data_key", "item_taken_id")
                try:
                    resp = requests.request(
                        take_spec.get("method", "POST"),
                        f"{base}{endpoint}",
                        headers={"Authorization": f"Bearer {main_token}"},
                        timeout=10,
                    )
                    if resp.status_code in (200, 201):
                        taken_id = cid
                        test_data[td_key] = cid
                        console.print(f"    Item {cid} taken ({td_key})")
                    else:
                        console.print(
                            f"    [yellow]Take failed ({resp.status_code})[/yellow]"
                        )
                except Exception as e:
                    console.print(f"    [yellow]Take error: {e}[/yellow]")

    return created_ids, taken_id


def _cleanup_test_data(
    app_url: str,
    test_data: dict,
    test_setup: dict,
    item_ids: list[str],
) -> None:
    """Clean up test items via manifest-configured API endpoint."""
    if not item_ids or not test_setup:
        return

    import requests

    base = app_url.rstrip("/")
    cleanup_endpoint = test_setup.get("cleanup_endpoint")
    if not cleanup_endpoint:
        return

    token = _api_login(base, test_setup, test_data)
    if not token:
        return
    headers = {"Authorization": f"Bearer {token}"}

    for item_id in item_ids:
        try:
            endpoint = _resolve_template(cleanup_endpoint, {"item_id": item_id})
            requests.post(f"{base}{endpoint}", headers=headers, timeout=10)
        except Exception as e:
            console.print(f"  [dim]Cleanup {item_id} failed: {e}[/dim]")

    console.print(f"  [dim]Cleaned up {len(item_ids)} test item(s)[/dim]")


async def _flutter_login(page: Page, app_url: str, email: str, otp: str = "1234") -> bool:
    """Authenticate on a Flutter CanvasKit app via coordinate-based interaction.

    Flow: /signin -> fill email -> click Connexion -> /login/otp -> fill OTP -> logged in.
    Returns True if login succeeded.
    """
    try:
        # Step 1: Navigate to signin
        await page.goto(f"{app_url.rstrip('/')}/signin", wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(3000)

        # Step 2: Click on the email field area to trigger Flutter's native input
        # Scan Y positions to find where the input appears
        for y in range(350, 600, 25):
            await page.mouse.click(195, y)
            await page.wait_for_timeout(300)
            inputs = await page.query_selector_all("input")
            if inputs:
                break
        else:
            console.print("    [yellow]Could not find email input field[/yellow]")
            return False

        # Step 3: Type the email
        inputs = await page.query_selector_all("input")
        if not inputs:
            return False
        await inputs[0].focus()
        await page.keyboard.type(email, delay=30)
        await page.wait_for_timeout(500)

        # Step 4: Click Connexion button (below the email field)
        await page.mouse.click(195, y + 80)
        await page.wait_for_timeout(5000)

        # Step 5: Check we're on OTP page
        if "/otp" not in page.url and "/login" not in page.url:
            # Might already be logged in
            if page.url.rstrip("/").endswith(app_url.rstrip("/")):
                return True

        # Step 6: Fill OTP digits
        otp_inputs = await page.query_selector_all("input")
        if len(otp_inputs) >= len(otp):
            for i, digit in enumerate(otp):
                await otp_inputs[i].focus()
                await page.keyboard.type(digit, delay=50)
                await page.wait_for_timeout(200)
        elif otp_inputs:
            await otp_inputs[0].focus()
            await page.keyboard.type(otp, delay=50)

        await page.wait_for_timeout(3000)

        # Check if we're logged in (URL should change from /login/otp)
        return "/otp" not in page.url and "/signin" not in page.url

    except Exception as e:
        console.print(f"    [yellow]Login error: {e}[/yellow]")
        return False



def _slugify(text: str) -> str:
    """Convert text to a filesystem-safe slug."""
    s = text.lower().strip()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_]+", "-", s)
    s = re.sub(r"-+", "-", s)
    return s.strip("-") or "page"


async def _extract_computed_styles(page: Page) -> list[dict] | None:
    """Extract computed styles from visible DOM elements.

    Returns None if DOM is not exploitable (e.g. Flutter CanvasKit).
    """
    try:
        styles = await page.evaluate("""() => {
            const elements = document.querySelectorAll('*');
            // Check if this is a CanvasKit app (Flutter renders into a canvas)
            const canvas = document.querySelector('canvas, flt-glass-pane');
            if (canvas && elements.length < 50) {
                return null;  // CanvasKit -- DOM not useful
            }

            const results = [];
            for (const el of elements) {
                const rect = el.getBoundingClientRect();
                if (rect.width === 0 || rect.height === 0) continue;
                if (rect.bottom < 0 || rect.top > window.innerHeight) continue;

                const style = window.getComputedStyle(el);
                const text = el.textContent?.trim().substring(0, 200) || '';

                // Only include elements with visible content
                if (!text && style.backgroundColor === 'rgba(0, 0, 0, 0)') continue;

                results.push({
                    tag: el.tagName.toLowerCase(),
                    text: text,
                    color: style.color,
                    backgroundColor: style.backgroundColor,
                    fontFamily: style.fontFamily,
                    fontSize: style.fontSize,
                    fontWeight: style.fontWeight,
                    padding: style.padding,
                    margin: style.margin,
                    borderRadius: style.borderRadius,
                    bounds: {
                        x: Math.round(rect.x),
                        y: Math.round(rect.y),
                        width: Math.round(rect.width),
                        height: Math.round(rect.height)
                    }
                });

                if (results.length >= 500) break;  // Limit
            }
            return results;
        }""")
        return styles
    except Exception as e:
        console.print(f"  [dim]DOM extraction failed: {e}[/dim]")
        return None


async def _capture_route(
    page: Page,
    page_info: dict,
    app_url: str,
    test_data: dict,
    screenshots_dir: Path,
) -> dict:
    """Capture a single route: navigate, screenshot, extract styles."""
    page_id = page_info["id"]
    route = page_info["route"]
    slug = _slugify(page_id)

    console.print(f"  Capturing {page_id} ({route})...")

    # Navigate
    nav_steps = page_info.get("navigation_steps", [])
    if nav_steps:
        for step in nav_steps:
            try:
                await _execute_navigation_step(page, step, test_data)
            except Exception as e:
                console.print(f"    [yellow]Nav step failed: {step.get('action')} -- {e}[/yellow]")
    else:
        # Simple direct navigation
        url = app_url.rstrip("/") + route
        try:
            await page.goto(url, wait_until="networkidle", timeout=15000)
        except Exception as e:
            console.print(f"    [yellow]Navigation failed: {e}[/yellow]")

    # Wait a bit for rendering to settle
    await page.wait_for_timeout(1000)

    # Screenshot
    screenshot_path = screenshots_dir / f"{slug}.png"
    await page.screenshot(path=str(screenshot_path), full_page=False)

    # Note: silent-redirect detection is handled globally after all captures
    # finish, via _dedupe_captures() which hashes every screenshot (including
    # wizard states) and flags any hash shared by 2+ captures as a navigation
    # failure. Doing it post-capture avoids the order-dependency of comparing
    # against a single reference image that may not exist yet.

    # Extract styles
    styles = await _extract_computed_styles(page)

    result = {
        "page_id": page_id,
        "route": route,
        "screenshot": f"app_screenshots/{slug}.png",
        "styles_available": styles is not None,
    }

    # Capture additional states (wizard steps, tabs) if defined
    capturable_states = page_info.get("capturable_states", [])
    if capturable_states:
        state_screenshots = []

        # First capturable state = the screenshot we already took
        first = capturable_states[0]
        state_screenshots.append({
            "state_id": first["state_id"],
            "screenshot": f"app_screenshots/{slug}.png",
        })

        # Subsequent states: execute delta_steps → screenshot
        for state_idx, state in enumerate(capturable_states[1:], start=2):
            state_id = state["state_id"]
            delta_steps = state.get("delta_steps", [])
            state_slug = f"{slug}--{_slugify(state_id)}"
            state_screenshot_rel = f"app_screenshots/{state_slug}.png"
            state_screenshot_path = screenshots_dir / f"{state_slug}.png"

            console.print(
                f"    State {state_idx}/{len(capturable_states)}: {state_id}..."
            )

            try:
                for step in delta_steps:
                    await _execute_navigation_step(page, step, test_data)
                await page.wait_for_timeout(1000)
                await page.screenshot(
                    path=str(state_screenshot_path), full_page=False
                )
                state_screenshots.append({
                    "state_id": state_id,
                    "screenshot": state_screenshot_rel,
                })
            except Exception as e:
                console.print(f"    [yellow]State {state_id} failed: {e}[/yellow]")
                state_screenshots.append({
                    "state_id": state_id,
                    "screenshot": None,
                    "error": str(e)[:200],
                })

        result["states"] = state_screenshots

    return result, styles


def _dedupe_captures(
    all_results: list[dict], screenshots_dir: Path
) -> tuple[int, int]:
    """Global silent-redirect detection.

    Hashes every screenshot file (top-level captures + wizard states) and flags
    silent navigation failures as follows:

    For each unique hash that appears in 2+ locations, we keep the capture
    whose ROUTE is shortest (and which is a top-level capture, not a wizard
    state) and mark every other occurrence as a navigation failure. The
    rationale: when multiple pages produce identical screenshots, the app
    silently redirected the deeper/more-specific routes to a shallower
    landing page (home, list, account menu, splash). The shortest route is
    the most plausible "legitimate" owner of that landing screen.

    Mutates ``all_results`` in place. Failed captures get ``screenshot=None``
    and ``error="Duplicate of <kept_page_id>'s screenshot — navigation
    likely failed"``. Wizard states that fall through to the same image as
    a sibling state (or another page) are flagged similarly.

    Returns ``(failed_captures, failed_states)`` counts.
    """
    import hashlib

    # Walk all captures, collecting (result_idx, state_idx_or_None, rel_path,
    # route_len, is_top_level) for every screenshot.
    locations: list[tuple[int, int | None, str, int, bool]] = []
    for i, result in enumerate(all_results):
        route = result.get("route") or ""
        route_len = len(route)
        top_path = result.get("screenshot")
        if top_path:
            locations.append((i, None, top_path, route_len, True))
        for s_idx, state in enumerate(result.get("states", []) or []):
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

    # Populate app with test data via API calls
    # Priority: config YAML test_setup > manifest test_setup (AI-generated, less reliable)
    test_setup = config.test_setup or pages_manifest.get("test_setup", {})
    created_item_ids: list[str] = []
    taken_item_id: str | None = None
    seed_account = config.seed_account.model_dump() if config.seed_account.email else None
    if test_setup.get("seed_items") and (seed_account or test_data.get("email")) and app_url:
        created_item_ids, taken_item_id = _setup_test_data(
            app_url, test_data, test_setup, seed_account=seed_account
        )
    # Note: _setup_test_data injects IDs directly into test_data
    # using the test_data_key from each seed_item spec in the manifest

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

        all_results = []
        all_styles = {}

        from figma_audit.utils.progress import get_progress

        run_progress = get_progress()
        total_pages = len(pages_to_capture)
        cap_idx = 0

        async def _capture_batch(batch: list[dict]) -> None:
            nonlocal cap_idx
            for page_info in batch:
                if run_progress:
                    run_progress.update(
                        step=f"{page_info['id']} ({page_info['route']})",
                        progress=cap_idx + 1,
                        total=total_pages,
                    )
                try:
                    result, styles = await _capture_route(
                        page, page_info, app_url, test_data, screenshots_dir
                    )
                    all_results.append(result)
                    if styles:
                        all_styles[page_info["id"]] = styles
                except Exception as e:
                    console.print(f"  [red]Error capturing {page_info['id']}: {e}[/red]")
                    all_results.append(
                        {
                            "page_id": page_info["id"],
                            "route": page_info["route"],
                            "screenshot": None,
                            "error": str(e),
                        }
                    )
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

            await _capture_batch(public_pages)

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
                console.print("  [green]Authentication successful[/green]")
            else:
                console.print(
                    "  [yellow]Authentication failed -- continuing without login[/yellow]"
                )

        # ── Phase C: Capture auth-required pages (after login) ────────
        if auth_pages:
            console.print(
                f"\n  [bold]Capturing {len(auth_pages)} authenticated page(s)...[/bold]"
            )
            await _capture_batch(auth_pages)

        await browser.close()

    # Cleanup test data
    if created_item_ids:
        cleanup_creds = seed_account if seed_account else test_data
        _cleanup_test_data(app_url, cleanup_creds, test_setup, created_item_ids)

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
