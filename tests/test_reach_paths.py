"""Tests for the reach_path selector in Phase 4.

The selector is pure (no Playwright) and picks the most appropriate
reach_path for a given browser state, falling back to legacy
navigation_steps when no path matches.
"""

from __future__ import annotations

from figma_audit.phases.capture_app.runner import _select_reach_path


class TestSelectReachPath:
    def test_no_reach_paths_returns_none(self):
        page = {"id": "x", "navigation_steps": [{"action": "navigate", "url": "/x"}]}
        assert _select_reach_path(page, is_authenticated=True) is None

    def test_empty_reach_paths_returns_none(self):
        page = {"id": "x", "reach_paths": []}
        assert _select_reach_path(page, is_authenticated=True) is None

    def test_picks_first_compatible_path(self):
        page = {
            "id": "x",
            "reach_paths": [
                {
                    "name": "guest_flow",
                    "required_auth": "guest",
                    "steps": [{"action": "navigate", "url": "/x"}],
                },
                {
                    "name": "authed_flow",
                    "required_auth": "authenticated",
                    "steps": [{"action": "navigate", "url": "/x-auth"}],
                },
            ],
        }
        # Logged in → picks the authenticated path (skips guest)
        assert _select_reach_path(page, is_authenticated=True)["name"] == "authed_flow"
        # Logged out → picks the guest path
        assert _select_reach_path(page, is_authenticated=False)["name"] == "guest_flow"

    def test_any_path_matches_both_states(self):
        page = {
            "id": "x",
            "reach_paths": [
                {
                    "name": "universal",
                    "required_auth": "any",
                    "steps": [{"action": "navigate", "url": "/x"}],
                },
            ],
        }
        assert _select_reach_path(page, is_authenticated=True)["name"] == "universal"
        assert _select_reach_path(page, is_authenticated=False)["name"] == "universal"

    def test_order_matters(self):
        """The agent lists paths from most preferred to least. The selector
        must respect that order, not pick by some other heuristic."""
        page = {
            "id": "x",
            "reach_paths": [
                {
                    "name": "preferred",
                    "required_auth": "any",
                    "steps": [{"action": "navigate", "url": "/preferred"}],
                },
                {
                    "name": "fallback",
                    "required_auth": "any",
                    "steps": [{"action": "navigate", "url": "/fallback"}],
                },
            ],
        }
        assert _select_reach_path(page, is_authenticated=True)["name"] == "preferred"

    def test_no_compatible_path_returns_none(self):
        """When every path requires the wrong auth state, fall back to None
        so the caller can use legacy navigation_steps or fail loudly."""
        page = {
            "id": "x",
            "reach_paths": [
                {
                    "name": "guest_only",
                    "required_auth": "guest",
                    "steps": [{"action": "navigate", "url": "/x"}],
                },
            ],
        }
        assert _select_reach_path(page, is_authenticated=True) is None

    def test_missing_required_auth_defaults_to_any(self):
        """Tolerate agent output that forgets required_auth: treat as `any`."""
        page = {
            "id": "x",
            "reach_paths": [
                {"name": "untagged", "steps": [{"action": "navigate", "url": "/x"}]},
            ],
        }
        assert _select_reach_path(page, is_authenticated=True)["name"] == "untagged"
        assert _select_reach_path(page, is_authenticated=False)["name"] == "untagged"
