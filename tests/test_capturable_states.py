"""Tests for Phase 4 capturable_states with query-based navigation.

The runner can now express tab-style states as query params instead of
a click sequence, so a Figma variant for "?tab=taken" gets its own
fresh navigation + screenshot. These tests cover the URL-merging logic.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


def _merge_query(current_url: str, overrides: dict) -> str:
    """Reproduce the query-merge logic from the runner.

    Kept as a small helper so the unit test does not depend on a real
    Playwright Page. The runner's inline implementation must match this
    behaviour byte for byte.
    """
    parsed = urlparse(current_url)
    merged = dict(parse_qsl(parsed.query, keep_blank_values=False))
    for k, v in overrides.items():
        if v == "" or v is None:
            merged.pop(k, None)
        else:
            merged[k] = str(v)
    return urlunparse(parsed._replace(query=urlencode(merged, doseq=False)))


class TestQueryMerge:
    def test_adds_param_to_url_without_query(self):
        result = _merge_query("https://app.test/orders", {"tab": "taken"})
        assert result == "https://app.test/orders?tab=taken"

    def test_replaces_existing_param(self):
        result = _merge_query("https://app.test/orders?tab=deposited", {"tab": "taken"})
        assert result == "https://app.test/orders?tab=taken"

    def test_keeps_other_params(self):
        result = _merge_query(
            "https://app.test/listings?in_stock=1&min_price=50", {"hide_sold": "1"}
        )
        # url params order may vary but content should match
        parsed = urlparse(result)
        assert parsed.path == "/listings"
        params = dict(parse_qsl(parsed.query))
        assert params == {"in_stock": "1", "min_price": "50", "hide_sold": "1"}

    def test_removes_param_when_value_empty(self):
        result = _merge_query("https://app.test/orders?tab=paid&hide=1", {"tab": ""})
        parsed = urlparse(result)
        assert dict(parse_qsl(parsed.query)) == {"hide": "1"}

    def test_preserves_path_with_route_params(self):
        """An already-templated path like /courses/342.xyz must survive."""
        result = _merge_query("https://app.test/listings/342.xyz/review", {"step": "2"})
        assert result.startswith("https://app.test/listings/342.xyz/review?")
        assert "step=2" in result

    def test_preserves_existing_query_when_overriding_one(self):
        result = _merge_query(
            "https://app.test/listings?date=TODAY&types=TYPE_1", {"date": "THIS_WEEK"}
        )
        parsed = urlparse(result)
        params = dict(parse_qsl(parsed.query))
        assert params == {"date": "THIS_WEEK", "types": "TYPE_1"}
