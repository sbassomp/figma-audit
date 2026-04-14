"""Unit tests for the navigation-step executor in capture_app.browser.

These cover the new ``bridge_push`` action and the Semantics / bridge
detection helpers. Playwright itself is mocked because we only want to
verify the step dispatching logic and the template resolution around it.
Tests drive the coroutines via ``asyncio.run`` so no additional pytest
plugin is required.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from figma_audit.phases.capture_app.browser import (
    _execute_navigation_step,
    _has_figma_audit_bridge,
    _has_flutter_semantics,
)


class _FakePage:
    """Minimal stand-in for ``playwright.async_api.Page``.

    ``evaluate`` is driven by a caller-supplied callable so each test can
    simulate bridge presence, Semantics availability, or push responses
    independently. The other async methods are no-ops.
    """

    def __init__(self, evaluate_impl=None, url: str = "https://app.test/"):
        self.url = url
        self._evaluate_impl = evaluate_impl or (lambda *a, **k: None)
        self.evaluate_calls: list[tuple[str, Any]] = []

    async def evaluate(self, script: str, arg: Any = None) -> Any:
        self.evaluate_calls.append((script, arg))
        result = self._evaluate_impl(script, arg)
        if asyncio.iscoroutine(result):
            result = await result
        return result

    async def goto(self, *a, **k) -> None:
        pass

    async def wait_for_timeout(self, *a, **k) -> None:
        pass

    async def wait_for_load_state(self, *a, **k) -> None:
        pass

    async def wait_for_selector(self, *a, **k) -> None:
        pass

    async def wait_for_url(self, *a, **k) -> None:
        pass


class TestBridgePush:
    def test_calls_bridge_when_installed(self) -> None:
        def ev(script, arg=None):
            # First call = bridge probe, returns True. Second = push, returns 'ok'.
            if arg is None and "figmaAudit" in script and "push" in script:
                return True
            return "ok"

        page = _FakePage(evaluate_impl=ev)
        step = {
            "action": "bridge_push",
            "url": "/courses/${course_id}/validate",
            "extra": {"course": {"id": "${course_id}", "label": "Test"}},
            "timeout": 1000,
        }
        asyncio.run(_execute_navigation_step(page, step, {"course_id": "319.x"}))

        # Two evaluate calls: probe + push
        assert len(page.evaluate_calls) == 2
        push_script, push_arg = page.evaluate_calls[1]
        assert "window.figmaAudit.push" in push_script
        route, extra_json = push_arg
        assert route == "/courses/319.x/validate"
        # Template in extra resolved before serialisation
        assert '"id": "319.x"' in extra_json

    def test_raises_when_bridge_missing(self) -> None:
        page = _FakePage(evaluate_impl=lambda *a, **k: False)
        step = {
            "action": "bridge_push",
            "url": "/courses/319/validate",
            "extra": {"x": 1},
        }
        with pytest.raises(RuntimeError, match="bridge is not installed"):
            asyncio.run(_execute_navigation_step(page, step, {}))

    def test_unresolved_template_rejected(self) -> None:
        """An unresolved ``${key}`` in the URL must trip the placeholder guard."""
        page = _FakePage()
        step = {
            "action": "bridge_push",
            "url": "/courses/${missing_id}/validate",
            "extra": {},
        }
        with pytest.raises(Exception):  # UnresolvedPlaceholderError subclass
            asyncio.run(_execute_navigation_step(page, step, {}))


class TestHasBridgeDetection:
    def test_true_when_push_function_exists(self) -> None:
        page = _FakePage(evaluate_impl=lambda *a, **k: True)
        assert asyncio.run(_has_figma_audit_bridge(page)) is True

    def test_false_when_absent(self) -> None:
        page = _FakePage(evaluate_impl=lambda *a, **k: False)
        assert asyncio.run(_has_figma_audit_bridge(page)) is False

    def test_false_when_evaluate_raises(self) -> None:
        def boom(*a, **k):
            raise RuntimeError("detached frame")

        page = _FakePage(evaluate_impl=boom)
        assert asyncio.run(_has_figma_audit_bridge(page)) is False


class TestHasSemanticsDetection:
    def test_true_when_flt_semantics_present(self) -> None:
        page = _FakePage(evaluate_impl=lambda *a, **k: True)
        assert asyncio.run(_has_flutter_semantics(page)) is True

    def test_false_when_canvas_only(self) -> None:
        page = _FakePage(evaluate_impl=lambda *a, **k: False)
        assert asyncio.run(_has_flutter_semantics(page)) is False
