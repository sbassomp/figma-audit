"""Tests for the Phase 1 manifest validator.

Run #47 and #48 showed that the AI sometimes emits plausible-looking
but broken manifest fields: literal ``/:id`` URLs, hallucinated
``${ghost_user_id}`` templates, or a page tagged public while its only
reach_path requires authentication. This validator catches each case
so Phase 4 no longer has to discover them at capture time.
"""

from __future__ import annotations

from figma_audit.phases.analyze_code.validator import validate_manifest


def _codes(issues) -> list[str]:
    return [i.code for i in issues]


class TestLiteralRouteParam:
    def test_detects_colon_id_in_navigation_steps(self):
        manifest = {
            "pages": [
                {
                    "id": "order_detail",
                    "route": "/orders/:id",
                    "navigation_steps": [{"action": "navigate", "url": "/orders/:id"}],
                }
            ]
        }
        _, issues = validate_manifest(manifest)
        assert "literal_route_param" in _codes(issues)
        assert any(i.page_id == "order_detail" for i in issues)

    def test_detects_colon_param_in_reach_path_step(self):
        manifest = {
            "pages": [
                {
                    "id": "listing_detail",
                    "reach_paths": [
                        {
                            "name": "direct",
                            "required_auth": "any",
                            "steps": [
                                {"action": "navigate", "url": "/listings/:listingId"},
                            ],
                        }
                    ],
                }
            ]
        }
        _, issues = validate_manifest(manifest)
        assert "literal_route_param" in _codes(issues)

    def test_templated_url_is_accepted(self):
        manifest = {
            "pages": [
                {
                    "id": "ok",
                    "navigation_steps": [
                        {"action": "navigate", "url": "/orders/${order_id}"},
                    ],
                }
            ]
        }
        _, issues = validate_manifest(manifest)
        assert "literal_route_param" not in _codes(issues)

    def test_wait_for_url_glob_pattern_is_not_flagged(self):
        manifest = {
            "pages": [
                {
                    "id": "ok",
                    "reach_paths": [
                        {
                            "name": "r",
                            "required_auth": "authenticated",
                            "steps": [
                                {"action": "wait_for_url", "pattern": "**/orders/*"},
                            ],
                        }
                    ],
                }
            ]
        }
        _, issues = validate_manifest(manifest)
        assert "literal_route_param" not in _codes(issues)


class TestUserIdTemplates:
    def test_unknown_user_id_alias_errors(self):
        manifest = {
            "test_setup": {"accounts": {"buyer": {"email": "b@x"}}},
            "pages": [
                {
                    "id": "profile",
                    "navigation_steps": [
                        {"action": "navigate", "url": "/users/${driver_user_id}"},
                    ],
                }
            ],
        }
        _, issues = validate_manifest(manifest)
        codes = _codes(issues)
        assert "unknown_user_id_alias" in codes
        msg = next(i.message for i in issues if i.code == "unknown_user_id_alias")
        assert "buyer_user_id" in msg or "default_viewer_user_id" in msg

    def test_default_viewer_user_id_is_known(self):
        manifest = {
            "test_setup": {"accounts": {"buyer": {"email": "b@x"}}},
            "pages": [
                {
                    "id": "profile",
                    "navigation_steps": [
                        {"action": "navigate", "url": "/users/${default_viewer_user_id}"},
                    ],
                }
            ],
        }
        _, issues = validate_manifest(manifest)
        assert "unknown_user_id_alias" not in _codes(issues)

    def test_role_specific_user_id_is_known(self):
        manifest = {
            "test_setup": {"accounts": {"seller": {"email": "s@x"}}},
            "pages": [
                {
                    "id": "shop",
                    "navigation_steps": [
                        {"action": "navigate", "url": "/shops/${seller_user_id}"},
                    ],
                }
            ],
        }
        _, issues = validate_manifest(manifest)
        assert "unknown_user_id_alias" not in _codes(issues)

    def test_generic_user_id_is_known(self):
        manifest = {
            "pages": [
                {
                    "id": "profile",
                    "navigation_steps": [
                        {"action": "navigate", "url": "/me/${user_id}"},
                    ],
                }
            ]
        }
        _, issues = validate_manifest(manifest)
        assert "unknown_user_id_alias" not in _codes(issues)

    def test_non_user_id_templates_are_not_checked(self):
        """Only ``*_user_id`` keys are validated — other templates are
        allowed to come from seed steps whose shape the validator
        cannot know."""
        manifest = {
            "pages": [
                {
                    "id": "x",
                    "navigation_steps": [
                        {"action": "navigate", "url": "/orders/${order_id}"},
                    ],
                }
            ]
        }
        _, issues = validate_manifest(manifest)
        assert issues == []


class TestAuthRequiredAutofix:
    def test_autofix_when_all_reach_paths_authenticated(self):
        manifest = {
            "pages": [
                {
                    "id": "account",
                    "auth_required": False,
                    "reach_paths": [
                        {
                            "name": "via_menu",
                            "required_auth": "authenticated",
                            "steps": [{"action": "navigate", "url": "/account"}],
                        }
                    ],
                }
            ]
        }
        fixed, issues = validate_manifest(manifest)
        assert fixed["pages"][0]["auth_required"] is True
        assert "auth_required_mismatch" in _codes(issues)
        assert any(i.severity == "fixed" for i in issues)

    def test_no_autofix_when_a_reach_path_is_guest(self):
        manifest = {
            "pages": [
                {
                    "id": "signup",
                    "auth_required": False,
                    "reach_paths": [
                        {"name": "direct", "required_auth": "guest", "steps": []},
                        {"name": "logged", "required_auth": "authenticated", "steps": []},
                    ],
                }
            ]
        }
        fixed, issues = validate_manifest(manifest)
        assert fixed["pages"][0]["auth_required"] is False
        assert "auth_required_mismatch" not in _codes(issues)

    def test_no_autofix_when_already_true(self):
        manifest = {
            "pages": [
                {
                    "id": "account",
                    "auth_required": True,
                    "reach_paths": [
                        {"name": "r", "required_auth": "authenticated", "steps": []},
                    ],
                }
            ]
        }
        _, issues = validate_manifest(manifest)
        assert "auth_required_mismatch" not in _codes(issues)

    def test_no_autofix_without_reach_paths(self):
        manifest = {"pages": [{"id": "welcome", "auth_required": False}]}
        _, issues = validate_manifest(manifest)
        assert issues == []


class TestDuplicates:
    def test_duplicate_page_ids_error(self):
        manifest = {
            "pages": [
                {"id": "home"},
                {"id": "home"},
            ]
        }
        _, issues = validate_manifest(manifest)
        assert "duplicate_page_id" in _codes(issues)

    def test_duplicate_state_ids_within_a_page_error(self):
        manifest = {
            "pages": [
                {
                    "id": "orders",
                    "capturable_states": [
                        {"state_id": "taken", "query": {}},
                        {"state_id": "taken", "query": {"tab": "taken"}},
                    ],
                }
            ]
        }
        _, issues = validate_manifest(manifest)
        assert "duplicate_state_id" in _codes(issues)

    def test_distinct_state_ids_are_ok(self):
        manifest = {
            "pages": [
                {
                    "id": "orders",
                    "capturable_states": [
                        {"state_id": "taken", "query": {}},
                        {"state_id": "paid", "query": {"tab": "paid"}},
                    ],
                }
            ]
        }
        _, issues = validate_manifest(manifest)
        assert issues == []


class TestEmptyManifest:
    def test_empty_manifest_produces_no_issues(self):
        _, issues = validate_manifest({})
        assert issues == []

    def test_manifest_without_pages_key(self):
        _, issues = validate_manifest({"framework": "flutter"})
        assert issues == []
