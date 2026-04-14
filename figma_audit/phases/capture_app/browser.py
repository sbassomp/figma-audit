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
            hint = ""
            if text and not selector and x is None:
                # Text-only click is the CanvasKit failure mode we see most
                # often — tell the user exactly what to add so they can
                # fix it without guessing.
                if not await _has_flutter_semantics(page):
                    hint = (
                        " (no <flt-semantics> found — Flutter accessibility "
                        "tree is off; add SemanticsBinding.instance.ensureSemantics() "
                        "in main.dart, see docs/integrations/flutter)"
                    )
            console.print(
                f"    [yellow]Click failed: selector={selector} text={text} "
                f"x={x} y={y}{hint}[/yellow]"
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
    """Authenticate on a Flutter CanvasKit app via coordinate-based interaction.

    Flow: ``/signin`` → fill email → click Connexion → ``/login/otp`` → fill
    OTP → logged in. Returns ``True`` if login succeeded.

    Coordinates are scanned because Flutter CanvasKit does not expose a
    DOM that selectors can target reliably.
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
