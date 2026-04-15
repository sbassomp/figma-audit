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
from figma_audit.phases.capture_app.templates import (
    NavigationFailedError,
    UnresolvedPlaceholderError,
    _assert_url_resolved,
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
        # Missing bridge is a structural navigation failure: the runner
        # must abort the capture and refuse to screenshot the wrong page.
        with pytest.raises(NavigationFailedError, match="bridge is not installed"):
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


class TestRouteParamGuard:
    """The placeholder guard must catch literal route-parameter syntax
    (``/courses/:id``, ``/profile/:userId``) which means the agent
    emitted the route template itself instead of a real value.
    """

    def test_rejects_literal_route_param(self):
        with pytest.raises(UnresolvedPlaceholderError, match="literal route parameter"):
            _assert_url_resolved("https://app.test/courses/:id")

    def test_rejects_literal_route_param_in_middle(self):
        with pytest.raises(UnresolvedPlaceholderError):
            _assert_url_resolved("https://app.test/courses/:id/validate")

    def test_rejects_literal_route_param_before_query(self):
        with pytest.raises(UnresolvedPlaceholderError):
            _assert_url_resolved("https://app.test/profile/:userId?tab=edit")

    def test_allows_real_id_containing_colon(self):
        """A real id like '318.x-yZ' may contain a dot but NOT a leading
        colon, so it must not trip the guard."""
        # Should not raise
        _assert_url_resolved("https://app.test/courses/318.d-YCIJc93Vs")
        _assert_url_resolved("https://app.test/profile/4092a617-6f15-4dfc-9b16-14614669e530")

    def test_allows_port_colon(self):
        """Host:port colons must not be mistaken for a route param."""
        _assert_url_resolved("http://localhost:8321/projects")

    def test_rejects_trailing_route_param(self):
        with pytest.raises(UnresolvedPlaceholderError):
            _assert_url_resolved("https://app.test/items/:item_id")


class TestHardNavigationFailures:
    """Critical nav steps must raise NavigationFailedError so the runner
    refuses to take a screenshot of whatever the browser happens to show.
    """

    def test_wait_for_url_timeout_raises(self):
        class _P(_FakePage):
            async def wait_for_url(self_inner, *a, **k):
                raise TimeoutError("Timeout 5000ms exceeded.")

        page = _P()
        with pytest.raises(NavigationFailedError, match="wait_for_url"):
            asyncio.run(
                _execute_navigation_step(
                    page,
                    {"action": "wait_for_url", "pattern": "**/items/*", "timeout": 5000},
                    {},
                )
            )

    def test_navigate_failure_raises(self):
        class _P(_FakePage):
            async def goto(self_inner, *a, **k):
                raise TimeoutError("net::ERR_CONNECTION_REFUSED")

        page = _P()
        with pytest.raises(NavigationFailedError, match="navigate"):
            asyncio.run(
                _execute_navigation_step(
                    page, {"action": "navigate", "url": "https://app.test/x"}, {}
                )
            )


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


class _FakeLocator:
    """Stand-in for a Playwright Locator returned by ``get_by_role``.

    Supports ``.all()`` and exposes fake elements that record their clicks.
    """

    def __init__(self, elements: list[_FakeElement]) -> None:
        self._elements = elements

    async def all(self):
        return list(self._elements)


class _FakeElement:
    def __init__(self, box: dict):
        self._box = box
        self.clicked = False

    async def bounding_box(self):
        return self._box

    async def click(self, timeout=None):
        self.clicked = True


class _ClickablePage(_FakePage):
    """A FakePage that honours get_by_role by returning a controlled locator.

    Tests instantiate it with a map role->list of fake elements, then run
    the click step and check which fake element got ``clicked = True``.
    """

    def __init__(self, elements_by_role: dict[str, list[_FakeElement]]):
        super().__init__(evaluate_impl=lambda *a, **k: True)
        self._elements_by_role = elements_by_role

    def get_by_role(self, role, name=""):
        return _FakeLocator(list(self._elements_by_role.get(role, [])))

    def get_by_text(self, *a, **k):
        class Stub:
            first = type(
                "X",
                (),
                {"click": staticmethod(lambda *a, **k: asyncio.sleep(0))},
            )()

        return Stub()


class TestClickRoleIndex:
    """Explicit role + index + min_y filter on the click action.

    These tests patch the locator so we can check which fake element was
    picked without running a real browser. Asserts on ``clicked``.
    """

    def test_click_first_button(self):
        first = _FakeElement(box={"x": 0, "y": 100, "width": 340, "height": 50})
        second = _FakeElement(box={"x": 0, "y": 200, "width": 340, "height": 50})
        page = _ClickablePage({"button": [first, second]})
        asyncio.run(
            _execute_navigation_step(page, {"action": "click", "role": "button", "index": 0}, {})
        )
        assert first.clicked is True
        assert second.clicked is False

    def test_click_nth_button(self):
        elements = [
            _FakeElement(box={"x": 0, "y": 100, "width": 340, "height": 50}),
            _FakeElement(box={"x": 0, "y": 200, "width": 340, "height": 50}),
            _FakeElement(box={"x": 0, "y": 300, "width": 340, "height": 50}),
        ]
        page = _ClickablePage({"button": elements})
        asyncio.run(
            _execute_navigation_step(page, {"action": "click", "role": "button", "index": 2}, {})
        )
        assert elements[2].clicked is True
        assert elements[0].clicked is False
        assert elements[1].clicked is False

    def test_click_min_y_excludes_app_bar(self):
        """``min_y: 80`` drops every element whose bounding box is above 80.

        This is how chain navigation picks the first real list tile instead
        of the back button in the app bar.
        """
        app_bar_btn = _FakeElement(box={"x": 0, "y": 20, "width": 40, "height": 40})
        first_tile = _FakeElement(box={"x": 0, "y": 120, "width": 340, "height": 60})
        second_tile = _FakeElement(box={"x": 0, "y": 200, "width": 340, "height": 60})
        page = _ClickablePage({"button": [app_bar_btn, first_tile, second_tile]})
        asyncio.run(
            _execute_navigation_step(
                page,
                {"action": "click", "role": "button", "index": 0, "min_y": 80},
                {},
            )
        )
        assert app_bar_btn.clicked is False
        assert first_tile.clicked is True
        assert second_tile.clicked is False

    def test_click_index_out_of_range_does_nothing(self):
        only = _FakeElement(box={"x": 0, "y": 100, "width": 340, "height": 50})
        page = _ClickablePage({"button": [only]})
        asyncio.run(
            _execute_navigation_step(page, {"action": "click", "role": "button", "index": 5}, {})
        )
        assert only.clicked is False
