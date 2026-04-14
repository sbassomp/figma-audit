"""Playwright-side helpers used by Phase 4: navigation steps, login, DOM extraction.

This module contains everything that talks to the live browser. The
runner (:mod:`runner`) orchestrates these calls; this module exposes them
as small pure-async functions that take a :class:`playwright.async_api.Page`
plus structured input.
"""

from __future__ import annotations

import json

from playwright.async_api import Page
from rich.console import Console

from figma_audit.phases.capture_app.templates import (
    UnresolvedPlaceholderError,
    _assert_url_resolved,
    _resolve_payload,
    _resolve_template,
)

console = Console()


async def _has_figma_audit_bridge(page: Page) -> bool:
    """Return True if the page exposes ``window.figmaAudit`` (bridge installed).

    The bridge is a tiny JS surface the Flutter app registers when audit
    mode is enabled (see docs/integrations/flutter). Its presence means
    figma-audit can push routes with GoRouter ``extra`` objects, read the
    current route, and inject app state, instead of fighting with URL-only
    navigation.
    """
    try:
        return bool(
            await page.evaluate(
                "() => !!(window.figmaAudit && typeof window.figmaAudit.push === 'function')"
            )
        )
    except Exception:
        return False


async def _has_flutter_semantics(page: Page) -> bool:
    """Return True if Flutter's accessibility tree is enabled on the page.

    When the audited app is a Flutter CanvasKit app, the browser only
    sees a ``<canvas>`` and accessibility-based selectors (``getByRole``,
    ``getByLabel``) find nothing. Calling
    ``SemanticsBinding.instance.ensureSemantics()`` at app startup fixes
    this by injecting a parallel DOM of ``<flt-semantics>`` nodes that
    mirror the widget tree. This helper checks for that marker so the
    runner can warn the user when it is missing.
    """
    script = "() => !!document.querySelector('flt-semantics, flt-semantics-host, [flt-semantics]')"
    try:
        return bool(await page.evaluate(script))
    except Exception:
        return False


async def _execute_navigation_step(page: Page, step: dict, test_data: dict) -> None:
    """Execute a single navigation step from the manifest.

    Supported actions: ``navigate``, ``click``, ``fill``, ``wait``,
    ``wait_for_url``, ``screenshot``. The ``click`` and ``fill`` actions
    use a multi-strategy fallback (CSS selector → accessibility role →
    text content → coordinates) so they work on Flutter CanvasKit apps
    that do not expose a useful DOM.
    """
    action = step.get("action", "")
    timeout = step.get("timeout", 10000)

    if action == "navigate":
        url = step.get("url", "")
        # Resolve ${test_data.key} templates in URLs
        if "${" in url:
            url = _resolve_template(url, test_data)
        # Guard against placeholder leakage — see UnresolvedPlaceholderError
        _assert_url_resolved(url)
        if url.startswith("/"):
            url = page.url.split("/")[0] + "//" + page.url.split("/")[2] + url
        await page.goto(url, wait_until="networkidle", timeout=timeout)

    elif action == "bridge_push":
        # Route push via the figma-audit Flutter bridge.
        #
        # Used for pages that are only reachable through GoRouter's ``extra``
        # parameter (in-memory objects that cannot be serialised into a URL).
        # The bridge is a small Flutter-side helper the audited app installs
        # under ``window.figmaAudit`` — see docs/integrations/flutter.
        # Without it this action is a no-op that logs a clear error so the
        # user knows to wire the bridge up.
        url = step.get("url", "")
        extra = step.get("extra")
        if "${" in url:
            url = _resolve_template(url, test_data)
        _assert_url_resolved(url)
        if isinstance(extra, dict):
            extra = _resolve_payload(extra, test_data)

        if not await _has_figma_audit_bridge(page):
            raise RuntimeError(
                f"bridge_push('{url}') requires window.figmaAudit but the "
                "bridge is not installed on this app. Integrate "
                "figma_audit_bridge.dart (see docs/integrations/flutter) "
                "or rewrite this step as a UI click sequence."
            )

        extra_json = json.dumps(extra, ensure_ascii=False) if extra is not None else "null"
        await page.evaluate(
            "([route, extraJson]) => window.figmaAudit.push(route, extraJson)",
            [url, extra_json],
        )
        # Wait for Flutter to settle after the route change.
        await page.wait_for_timeout(500)
        try:
            await page.wait_for_load_state("networkidle", timeout=timeout)
        except Exception:
            pass

    elif action == "click":
        # Click resolution order:
        # 1. CSS selector (traditional DOM apps)
        # 2. Explicit Semantics role + optional text + optional index/min_y
        #    (the "click the Nth button below the app bar" pattern used for
        #    chain navigation like "open invoices list, click the first tile")
        # 3. Text-based sweep across common interactive roles (legacy)
        # 4. Plain text match anywhere (HTML renderer)
        # 5. Coordinate fallback
        selector = step.get("selector", "")
        text = step.get("text", "")
        role = step.get("role", "")
        index = int(step.get("index", 0) or 0)
        min_y = step.get("min_y")
        x, y = step.get("x"), step.get("y")
        clicked = False

        if selector and not clicked:
            try:
                await page.click(selector, timeout=min(timeout, 3000))
                clicked = True
            except Exception:
                pass

        # Explicit Semantics role: the agent (or a manual step) tells us
        # exactly which role to look for and which match to pick. When no
        # text is given, this matches every element with that role, in
        # document order. The ``min_y`` filter rejects hits inside the top
        # chrome (app bar, status bar) so "first list item" lands on the
        # first real content tile, not the back button.
        if role and not clicked:
            try:
                locator = page.get_by_role(role, name=text) if text else page.get_by_role(role)
                candidates = await locator.all()
                if min_y is not None:
                    filtered = []
                    for el in candidates:
                        try:
                            box = await el.bounding_box()
                        except Exception:
                            continue
                        if box and box.get("y", 0) >= min_y:
                            filtered.append(el)
                    candidates = filtered
                if len(candidates) > index:
                    await candidates[index].click(timeout=min(timeout, 3000))
                    clicked = True
                elif candidates:
                    # Fewer matches than the requested index: clearer error
                    console.print(
                        f"    [yellow]click role='{role}' index={index} but only "
                        f"{len(candidates)} match(es) available[/yellow]"
                    )
            except Exception as e:
                console.print(f"    [dim]role click failed: {e}[/dim]")

        # Strategy 3: text-based sweep across common roles (legacy helper
        # for pages that only know the visible label, no explicit role)
        if text and not clicked:
            for guess_role in ("button", "link", "tab", "menuitem"):
                try:
                    await page.get_by_role(guess_role, name=text).click(timeout=min(timeout, 3000))
                    clicked = True
                    break
                except Exception:
                    pass

        # Strategy 4: text match anywhere (DOM / HTML renderer fallback)
        if text and not clicked:
            try:
                await page.get_by_text(text, exact=False).first.click(timeout=min(timeout, 3000))
                clicked = True
            except Exception:
                pass

        # Strategy 5: coordinate-based (last resort)
        if x is not None and y is not None and not clicked:
            try:
                await page.mouse.click(x, y)
                clicked = True
            except Exception:
                pass

        if not clicked:
            hint = ""
            if (text or role) and not selector and x is None:
                # Text-only click is the CanvasKit failure mode we see most
                # often. Tell the user exactly what to add so they can
                # fix it without guessing.
                if not await _has_flutter_semantics(page):
                    hint = (
                        " (no <flt-semantics> found; Flutter accessibility "
                        "tree is off; add SemanticsBinding.instance.ensureSemantics() "
                        "in main.dart, see docs/integrations/flutter)"
                    )
            console.print(
                f"    [yellow]Click failed: selector={selector} role={role} text={text} "
                f"index={index} x={x} y={y}{hint}[/yellow]"
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


async def _flutter_login(page: Page, app_url: str, email: str, otp: str = "1234") -> bool:
    """Authenticate on a Flutter web app.

    Dispatches between two strategies:

    - **Semantics-first** (preferred): the audited app has enabled the
      Flutter accessibility tree (see ``docs/integrations/flutter``) so
      the login form is queryable via Playwright's accessibility APIs.
      This path is stable against layout shifts because it targets nodes
      by role and text content rather than hard-coded pixel coordinates.
    - **Coordinate fallback**: the legacy flow that scans Y positions to
      find the email input. Only used when Semantics is unavailable.
      Fragile — any layout tweak on the audited app breaks it.

    Returns ``True`` if the final URL is no longer ``/signin``, ``/login``
    or ``/otp``, which for a Flutter web app reliably signals a real
    session has been established.
    """
    try:
        await page.goto(f"{app_url.rstrip('/')}/signin", wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(2500)
    except Exception as e:
        console.print(f"    [yellow]Login navigation failed: {e}[/yellow]")
        return False

    if await _has_flutter_semantics(page):
        return await _flutter_login_semantics(page, app_url, email, otp)
    console.print(
        "    [yellow]Flutter Semantics not detected — falling back to coordinate-based "
        "login (fragile). Add SemanticsBinding.instance.ensureSemantics() to main.dart, "
        "see docs/integrations/flutter.[/yellow]"
    )
    return await _flutter_login_coords(page, app_url, email, otp)


async def _flutter_login_semantics(page: Page, app_url: str, email: str, otp: str = "1234") -> bool:
    """Semantics-first login path.

    Steps:

    1. Locate the Email text node in the accessibility tree and click
       slightly below its center. Flutter routes the click to the
       underlying ``TextField`` and opens its hidden keyboard input.
    2. Type the email via ``keyboard.type``. Using ``.fill()`` on the
       DOM input would not trigger Flutter's input events and leave the
       widget empty.
    3. Click the Connexion button via ``get_by_role`` (layout-agnostic).
    4. Wait for navigation to the OTP screen.
    5. Type the whole OTP string; Flutter distributes the digits across
       the individual boxes and auto-submits on completion.
    6. Confirm the URL left ``/signin``, ``/login`` and ``/otp``.
    """
    try:
        # Step 1: click the Email label's semantic node, offset below to
        # hit the real TextField (the Semantics text node covers only the
        # label, not the field beneath it).
        email_target = await page.evaluate(
            """() => {
                const nodes = Array.from(document.querySelectorAll('flt-semantics'));
                for (const n of nodes) {
                    const t = (n.textContent || '').trim();
                    if (t.startsWith('Email') || t.toLowerCase().startsWith('e-mail')) {
                        const r = n.getBoundingClientRect();
                        return {x: Math.round(r.x + r.width / 2),
                                y: Math.round(r.y + r.height / 2 + 25)};
                    }
                }
                return null;
            }"""
        )
        if not email_target:
            console.print("    [yellow]Semantics: no Email label found on /signin[/yellow]")
            return False
        await page.mouse.click(email_target["x"], email_target["y"])
        await page.wait_for_timeout(400)

        # Step 2: type email via keyboard
        await page.keyboard.type(email, delay=30)
        await page.wait_for_timeout(500)

        # Step 3: click Connexion button via accessibility role
        clicked = False
        for name in ("Connexion", "Se connecter", "Continuer", "Valider", "Suivant"):
            try:
                await page.get_by_role("button", name=name).first.click(timeout=3000)
                clicked = True
                break
            except Exception:
                continue
        if not clicked:
            console.print("    [yellow]Semantics: no submit button matched[/yellow]")
            return False

        # Step 4: wait for transition to /otp or direct landing
        try:
            await page.wait_for_url(
                lambda u: "/signin" not in u or "/otp" in u or "/login" in u,
                timeout=8000,
            )
        except Exception:
            pass
        await page.wait_for_timeout(1000)

        # Already logged in? (e.g. saved session or empty OTP flow)
        if "/signin" not in page.url and "/otp" not in page.url and "/login" not in page.url:
            return True

        # Step 5: type OTP. Flutter's 6-digit grid auto-focuses the first
        # field and distributes subsequent digits across the rest.
        await page.keyboard.type(otp, delay=80)
        await page.wait_for_timeout(3500)

        # Step 6: verify we're actually logged in now
        return "/signin" not in page.url and "/otp" not in page.url and "/login" not in page.url
    except Exception as e:
        console.print(f"    [yellow]Semantics login error: {e}[/yellow]")
        return False


async def _flutter_login_coords(page: Page, app_url: str, email: str, otp: str = "1234") -> bool:
    """Legacy coordinate-based login flow.

    Kept as a fallback for apps that have not enabled Flutter Semantics.
    Fragile: any layout change on the audited app shifts the pixel
    offsets and breaks the click sequence. Prefer the semantics path.
    """
    try:
        # Click on the email field area to trigger Flutter's native input
        for y in range(350, 600, 25):
            await page.mouse.click(195, y)
            await page.wait_for_timeout(300)
            inputs = await page.query_selector_all("input")
            if inputs:
                break
        else:
            console.print("    [yellow]Could not find email input field[/yellow]")
            return False

        # Type the email
        inputs = await page.query_selector_all("input")
        if not inputs:
            return False
        await inputs[0].focus()
        await page.keyboard.type(email, delay=30)
        await page.wait_for_timeout(500)

        # Click Connexion button (below the email field)
        await page.mouse.click(195, y + 80)
        await page.wait_for_timeout(5000)

        # Check we're on OTP page
        if "/otp" not in page.url and "/login" not in page.url:
            if page.url.rstrip("/").endswith(app_url.rstrip("/")):
                return True

        # Fill OTP digits
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

        return "/otp" not in page.url and "/signin" not in page.url

    except Exception as e:
        console.print(f"    [yellow]Login error: {e}[/yellow]")
        return False


async def _extract_computed_styles(page: Page) -> list[dict] | None:
    """Extract computed styles from visible DOM elements.

    Returns ``None`` if the DOM is not exploitable (e.g. Flutter CanvasKit).
    """
    try:
        styles = await page.evaluate(
            """() => {
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
        }"""
        )
        return styles
    except Exception as e:
        console.print(f"  [dim]DOM extraction failed: {e}[/dim]")
        return None


# Re-export so callers in this package can `from .browser import UnresolvedPlaceholderError`
__all__ = [
    "UnresolvedPlaceholderError",
    "_execute_navigation_step",
    "_extract_computed_styles",
    "_flutter_login",
]
