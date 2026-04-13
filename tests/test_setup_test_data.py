"""Unit tests for the Phase C helpers in figma_audit.phases.setup_test_data.

These cover the pure functions (account derivation, agent-output
normalization). The agent loop itself is not exercised here — that's a
network-dependent integration test.
"""

from __future__ import annotations

import pytest

from figma_audit.config import Account, Config, SeedAccountConfig, TestCredentials, TestSetup
from figma_audit.phases.setup_test_data import (
    _derive_accounts,
    _normalize_agent_output,
)


class TestDeriveAccounts:
    def test_new_shape_accounts_take_priority(self):
        ts = TestSetup(
            accounts={
                "seller": Account(email="seller@x"),
                "buyer": Account(email="buyer@x"),
            }
        )
        cfg = Config(
            seed_account=SeedAccountConfig(email="legacy-seed@x"),
            test_credentials=TestCredentials(email="legacy-main@x"),
        )
        accounts = _derive_accounts(cfg, ts)
        assert set(accounts.keys()) == {"seller", "buyer"}
        assert accounts["seller"].email == "seller@x"

    def test_legacy_fallback_uses_seed_and_main(self):
        """Without explicit accounts, derive `seed` and `main` from Config."""
        ts = TestSetup()  # empty
        cfg = Config(
            seed_account=SeedAccountConfig(email="seed@x", otp="1111"),
            test_credentials=TestCredentials(email="main@x", otp="2222"),
        )
        accounts = _derive_accounts(cfg, ts)
        assert set(accounts.keys()) == {"seed", "main"}
        assert accounts["seed"].otp == "1111"
        assert accounts["main"].otp == "2222"

    def test_legacy_fallback_partial(self):
        """Only seed_account is configured → only `seed` is derived."""
        ts = TestSetup()
        cfg = Config(seed_account=SeedAccountConfig(email="seed@x"))
        accounts = _derive_accounts(cfg, ts)
        assert list(accounts.keys()) == ["seed"]

    def test_no_accounts_anywhere(self):
        assert _derive_accounts(Config(), TestSetup()) == {}


class TestNormalizeAgentOutput:
    def test_accepts_new_shape_from_agent(self):
        agent_output = {
            "auth_endpoint": "/api/auth/login",
            "auth_payload": {"email": "${email}", "otp": "${otp}"},
            "steps": [
                {
                    "name": "create_listing",
                    "as": "seller",
                    "endpoint": "/api/listings",
                    "payload": {"title": "t"},
                    "save": {"listing_id": "id"},
                },
                {
                    "name": "place_order",
                    "as": "buyer",
                    "endpoint": "/api/listings/${listing_id}/orders",
                    "payload": {"qty": 1},
                    "save": {"order_id": "id"},
                    "depends_on": ["create_listing"],
                },
            ],
            "default_viewer": "buyer",
        }
        accounts = {
            "seller": Account(email="seller@x", otp="1111"),
            "buyer": Account(email="buyer@x", otp="2222"),
        }
        normalized = _normalize_agent_output(agent_output, accounts)

        assert normalized["auth_endpoint"] == "/api/auth/login"
        assert normalized["default_viewer"] == "buyer"
        # The injected accounts (with real credentials) overwrite anything
        # the agent may have emitted for that field.
        assert normalized["accounts"]["seller"]["email"] == "seller@x"
        assert normalized["accounts"]["buyer"]["otp"] == "2222"
        # Steps preserve role assignment via the YAML-friendly `as` key.
        assert normalized["steps"][0]["as"] == "seller"
        assert normalized["steps"][1]["as"] == "buyer"
        assert normalized["steps"][1]["depends_on"] == ["create_listing"]

    def test_auto_migrates_legacy_shape_from_agent(self):
        """If the agent returns the old seed_items / take_item format, we
        migrate it using the provided accounts."""
        legacy = {
            "auth_endpoint": "/api/auth/login",
            "auth_payload": {"email": "${email}"},
            "seed_items": [
                {
                    "endpoint": "/api/courses",
                    "payload": {"title": "t"},
                    "test_data_key": "course_id",
                    "id_path": "id",
                },
            ],
            "take_item": {
                "endpoint": "/api/courses/${course_id}/take",
                "method": "POST",
                "test_data_key": "taken_id",
                "id_path": "id",
            },
        }
        accounts = {
            "seed": Account(email="seed@x"),
            "main": Account(email="main@x"),
        }
        normalized = _normalize_agent_output(legacy, accounts)

        assert len(normalized["steps"]) == 2
        assert normalized["steps"][0]["as"] == "seed"
        assert normalized["steps"][1]["as"] == "main"
        assert normalized["steps"][1]["depends_on"] == ["course_id"]

    def test_rejects_step_referencing_unknown_role(self):
        """If the agent tags a step with a role that isn't in our registered
        accounts, normalization must fail loudly — we cannot write an
        unrunnable config to YAML."""
        agent_output = {
            "steps": [
                {
                    "name": "s",
                    "as": "driver",  # not in accounts
                    "endpoint": "/api/x",
                },
            ],
        }
        accounts = {"buyer": Account(email="b@x"), "seller": Account(email="s@x")}
        with pytest.raises(ValueError, match="unknown role 'driver'"):
            _normalize_agent_output(agent_output, accounts)
