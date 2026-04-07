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
        if url.startswith("/"):
            url = page.url.split("/")[0] + "//" + page.url.split("/")[2] + url
        await page.goto(url, wait_until="networkidle", timeout=timeout)

    elif action == "click":
        selector = step.get("selector", "")
        try:
            await page.click(selector, timeout=timeout)
        except Exception as e:
            # Fallback: try text-based selector
            text = step.get("text", "")
            if text:
                await page.get_by_text(text).click(timeout=timeout)
            else:
                console.print(f"    [dim]Click failed on {selector}: {e}[/dim]")

    elif action == "fill":
        selector = step.get("selector", "")
        value = step.get("value", "")
        # Resolve test_data references
        if value.startswith("${") and value.endswith("}"):
            key = value[2:-1]
            value = _resolve_test_data(test_data, key)
        try:
            await page.fill(selector, value, timeout=timeout)
        except Exception as e:
            try:
                await page.locator(selector).fill(value, timeout=timeout)
            except Exception:
                console.print(f"    [dim]Fill failed on {selector}: {e}[/dim]")

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


def _setup_test_data(app_url: str, test_data: dict, seed_account: dict | None = None) -> list[str]:
    """Populate the app with test courses via API. Returns list of created course IDs.

    Uses seed_account (a different user) so courses appear in "available" for the main user.
    Falls back to main test_data credentials if no seed_account.
    """
    from datetime import datetime, timedelta, timezone

    import requests

    base = app_url.rstrip("/") + "/api"
    email = (seed_account or {}).get("email") or test_data.get("email")
    otp = (seed_account or {}).get("otp") or test_data.get("otp", "1234")

    console.print("  [bold]Setting up test data via API...[/bold]")

    # Step 1: Get auth token
    try:
        requests.post(
            f"{base}/public/auth/login/request-otp-email",
            json={"email": email},
            timeout=10,
        )
        resp = requests.post(
            f"{base}/public/auth/login/verify-otp-email",
            json={"email": email, "code": otp},
            timeout=10,
        )
        if resp.status_code != 200:
            console.print(
                f"    [yellow]API login failed ({resp.status_code}): {resp.text[:100]}[/yellow]"
            )
            return []
        body = resp.json()
        token = body.get("accessToken") or body.get("access_token")
        if not token:
            console.print(f"    [yellow]No token in API response: {list(body.keys())}[/yellow]")
            return []
        console.print("    API login OK")
    except Exception as e:
        console.print(f"    [yellow]API login error: {e}[/yellow]")
        return []

    headers = {"Authorization": f"Bearer {token}"}
    tomorrow = datetime.now(timezone.utc) + timedelta(days=1)

    def _fmt_dt(hour: int, minute: int = 0) -> str:
        return tomorrow.replace(hour=hour, minute=minute, second=0, microsecond=0).strftime(
            "%Y-%m-%dT%H:%M:%S.000Z"
        )

    courses = [
        {
            "departureLat": 43.2965,
            "departureLng": 5.3698,
            "departureAddress": "13012 St Julien Marseille",
            "destinationLat": 43.2505,
            "destinationLng": 5.4013,
            "destinationAddress": "La Timone Marseille",
            "destinationIsHealthcareFacility": True,
            "desiredArrivalTime": _fmt_dt(10),
            "waitingTimeMinutes": 15,
            "courseType": "TYPE_1",
            "visibility": "PUBLIC",
            "roundTrip": True,
            "patientReady": True,
        },
        {
            "departureLat": 43.3004,
            "departureLng": 5.3810,
            "departureAddress": "13400 Napollon Aubagne",
            "destinationLat": 43.2505,
            "destinationLng": 5.4013,
            "destinationAddress": "La Timone Marseille",
            "destinationIsHealthcareFacility": True,
            "desiredArrivalTime": _fmt_dt(14, 30),
            "waitingTimeMinutes": 10,
            "courseType": "TYPE_1",
            "visibility": "PUBLIC",
            "urgency": True,
        },
    ]

    created_ids = []
    for i, course_data in enumerate(courses):
        try:
            resp = requests.post(
                f"{base}/exchange/courses",
                json=course_data,
                headers=headers,
                timeout=10,
            )
            if resp.status_code in (200, 201):
                course_id = resp.json().get("id")
                if course_id:
                    created_ids.append(str(course_id))
                console.print(f"    Course {i + 1} created")
            else:
                console.print(
                    f"    [yellow]Course {i + 1} failed "
                    f"({resp.status_code}): "
                    f"{resp.text[:100]}[/yellow]"
                )
        except Exception as e:
            console.print(f"    [yellow]Course {i + 1} error: {e}[/yellow]")

    console.print(f"  [green]{len(created_ids)} test course(s) created[/green]")
    return created_ids


def _cleanup_test_data(app_url: str, test_data: dict, course_ids: list[str]) -> None:
    """Archive test courses created during capture."""
    if not course_ids:
        return

    import requests

    base = app_url.rstrip("/") + "/api"
    email = test_data.get("email")
    otp = test_data.get("otp", "1234")

    try:
        requests.post(
            f"{base}/public/auth/login/request-otp-email",
            json={"email": email},
            timeout=10,
        )
        resp = requests.post(
            f"{base}/public/auth/login/verify-otp-email",
            json={"email": email, "code": otp},
            timeout=10,
        )
        body = resp.json()
        token = body.get("accessToken") or body.get("access_token")
        headers = {"Authorization": f"Bearer {token}"}

        for cid in course_ids:
            try:
                requests.post(
                    f"{base}/exchange/courses/{cid}/archive",
                    headers=headers,
                    timeout=10,
                )
            except Exception as e:
                console.print(f"  [dim]Cleanup course {cid} failed: {e}[/dim]")

        console.print(f"  [dim]Cleaned up {len(course_ids)} test course(s)[/dim]")
    except Exception as e:
        console.print(f"  [dim]Cleanup skipped: {e}[/dim]")


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


def _resolve_test_data(test_data: dict, key: str) -> str:
    """Resolve a dotted key path in test_data (e.g. 'addresses.pickup')."""
    parts = key.split(".")
    current = test_data
    for part in parts:
        if isinstance(current, dict):
            current = current.get(part, "")
        else:
            return str(current)
    return str(current)


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

    # Extract styles
    styles = await _extract_computed_styles(page)

    result = {
        "page_id": page_id,
        "route": route,
        "screenshot": f"app_screenshots/{slug}.png",
        "styles_available": styles is not None,
    }

    # Capture interactive states if defined
    interactive_states = page_info.get("interactive_states", [])
    state_screenshots = []
    for state in interactive_states[1:]:  # Skip first state (already captured)
        # We can only capture additional states if we have navigation steps for them
        # For now, just note them
        state_screenshots.append({"state": state, "screenshot": None})

    if state_screenshots:
        result["states"] = state_screenshots

    return result, styles


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

    # Populate app with test data via API (if credentials available)
    created_course_ids = []
    seed_account = config.seed_account.model_dump() if config.seed_account.email else None
    if (seed_account or test_data.get("email")) and app_url:
        created_course_ids = _setup_test_data(app_url, test_data, seed_account=seed_account)

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

        # Initial navigation and authentication
        console.print(f"  Loading {app_url}...")
        try:
            await page.goto(app_url, wait_until="networkidle", timeout=30000)
        except Exception as e:
            console.print(f"  [yellow]Initial load: {e}[/yellow]")

        # Authenticate if test credentials are available
        auth_email = test_data.get("email") or test_data.get("phone")
        auth_otp = test_data.get("otp", "1234")
        if auth_email:
            logged_in = await _flutter_login(page, app_url, auth_email, auth_otp)
            if logged_in:
                console.print("  [green]Authentication successful[/green]")
            else:
                console.print(
                    "  [yellow]Authentication failed -- continuing without login[/yellow]"
                )

        # Capture each page
        all_results = []
        all_styles = {}

        for page_info in pages_to_capture:
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

        await browser.close()

    # Cleanup test data
    if created_course_ids:
        cleanup_data = seed_account if seed_account else test_data
        _cleanup_test_data(app_url, cleanup_data, created_course_ids)

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
    console.print(f"  {captured} screenshots saved to {screenshots_dir}")
    if all_styles:
        console.print(f"  {len(all_styles)} pages with DOM styles extracted")
    if errors:
        console.print(f"  [yellow]{errors} pages with errors[/yellow]")

    return captures_path


def run(config: Config) -> Path:
    """Run Phase 4: Capture app screenshots via Playwright."""
    return asyncio.run(_run_async(config))
