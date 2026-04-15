"""Tests for configuration loading."""

import os
import tempfile
from pathlib import Path

import pytest
import yaml

from figma_audit.config import Account, Config, SetupStep, TestSetup


class TestConfigFileKey:
    def test_extract_from_design_url(self):
        cfg = Config(figma_url="https://www.figma.com/design/6kTFQMSueuk1dSDgiuMur9/ExampleApp")
        assert cfg.figma_file_key == "6kTFQMSueuk1dSDgiuMur9"

    def test_extract_from_file_url(self):
        cfg = Config(figma_url="https://www.figma.com/file/abc123/MyFile")
        assert cfg.figma_file_key == "abc123"

    def test_no_url(self):
        cfg = Config()
        assert cfg.figma_file_key is None

    def test_invalid_url(self):
        cfg = Config(figma_url="https://example.com/nothing")
        assert cfg.figma_file_key is None


class TestConfigPaths:
    def test_output_dir(self):
        cfg = Config(output="./my-output")
        assert cfg.output_dir.name == "my-output"
        assert cfg.output_dir.is_absolute()

    def test_figma_cache_dir(self):
        cfg = Config(output="./out")
        assert cfg.figma_cache_dir == cfg.output_dir / "figma_raw"

    def test_figma_screens_dir(self):
        cfg = Config(output="./out")
        assert cfg.figma_screens_dir == cfg.output_dir / "figma_screens"


class TestConfigLoad:
    def test_load_from_yaml(self):
        data = {
            "project": "/tmp/myproject",
            "figma_url": "https://www.figma.com/design/abc123/Test",
            "app_url": "https://myapp.example.com",
            "output": "/tmp/audit",
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(data, f)
            path = Path(f.name)

        try:
            cfg = Config.load(config_path=path)
            assert cfg.project == "/tmp/myproject"
            assert cfg.figma_file_key == "abc123"
            assert cfg.app_url == "https://myapp.example.com"
        finally:
            path.unlink()

    def test_cli_overrides(self):
        cfg = Config.load(output="/tmp/override", app_url="https://override.com")
        assert cfg.output == "/tmp/override"
        assert cfg.app_url == "https://override.com"

    def test_none_overrides_ignored(self):
        cfg = Config.load(output=None, app_url=None)
        assert cfg.output == "./audit-results"  # default
        assert cfg.app_url is None

    def test_env_var_fallback(self):
        os.environ["FIGMA_TOKEN"] = "figd_test_token"
        try:
            cfg = Config.load()
            assert cfg.figma_token == "figd_test_token"
        finally:
            del os.environ["FIGMA_TOKEN"]

    def test_env_var_resolution(self):
        os.environ["MY_KEY"] = "resolved_value"
        try:
            cfg = Config(anthropic_api_key="${MY_KEY}")
            assert cfg.anthropic_api_key == "resolved_value"
        finally:
            del os.environ["MY_KEY"]


class TestSeedAccount:
    def test_default_empty(self):
        cfg = Config()
        assert cfg.seed_account.email is None
        assert cfg.seed_account.otp == "1234"

    def test_from_yaml(self):
        data = {"seed_account": {"email": "test@example.com", "otp": "5678"}}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(data, f)
            path = Path(f.name)

        try:
            cfg = Config.load(config_path=path)
            assert cfg.seed_account.email == "test@example.com"
            assert cfg.seed_account.otp == "5678"
        finally:
            path.unlink()


class TestAccountModel:
    def test_defaults(self):
        a = Account()
        assert a.email is None
        assert a.otp == "1234"
        assert a.auth_payload is None

    def test_with_credentials(self):
        a = Account(email="buyer@example.com", otp="0000")
        assert a.email == "buyer@example.com"
        assert a.otp == "0000"


class TestSetupStepModel:
    def test_as_alias_yaml_style(self):
        """YAML uses ``as:`` which maps to Python attribute ``as_role``."""
        step = SetupStep(
            name="create_listing",
            **{"as": "seller"},
            endpoint="/api/listings",
        )
        assert step.as_role == "seller"
        assert step.method == "POST"  # default
        assert step.payload == {}
        assert step.depends_on == []

    def test_populate_by_name_also_works(self):
        step = SetupStep(
            name="s",
            as_role="seller",
            endpoint="/api/x",
        )
        assert step.as_role == "seller"


class TestTestSetupNewShape:
    def test_empty_parses(self):
        ts = TestSetup.from_raw({})
        assert ts.accounts == {}
        assert ts.steps == []
        assert ts.default_viewer is None

    def test_new_shape_strips_runtime_keys(self):
        raw = {
            "accounts": {"buyer": {"email": "b@x"}},
            "default_viewer": "buyer",
            "_api_prefix_hint": "/api",  # runtime noise — must be stripped
        }
        ts = TestSetup.from_raw(raw)
        assert "buyer" in ts.accounts
        assert ts.default_viewer == "buyer"

    def test_rejects_step_with_unknown_role(self):
        raw = {
            "accounts": {"buyer": {"email": "b@x"}},
            "steps": [
                {"name": "s1", "as": "seller", "endpoint": "/api/x"},  # seller undeclared
            ],
        }
        with pytest.raises(ValueError, match="unknown role 'seller'"):
            TestSetup.from_raw(raw)

    def test_rejects_unknown_depends_on(self):
        raw = {
            "accounts": {"seller": {"email": "s@x"}},
            "steps": [
                {"name": "s1", "as": "seller", "endpoint": "/api/x", "depends_on": ["ghost"]},
            ],
        }
        with pytest.raises(ValueError, match="depends on unknown step 'ghost'"):
            TestSetup.from_raw(raw)

    def test_rejects_cycle(self):
        raw = {
            "accounts": {"seller": {"email": "s@x"}},
            "steps": [
                {"name": "a", "as": "seller", "endpoint": "/api/a", "depends_on": ["b"]},
                {"name": "b", "as": "seller", "endpoint": "/api/b", "depends_on": ["a"]},
            ],
        }
        with pytest.raises(ValueError, match="Cycle detected"):
            TestSetup.from_raw(raw)

    def test_rejects_duplicate_step_names(self):
        raw = {
            "accounts": {"seller": {"email": "s@x"}},
            "steps": [
                {"name": "s", "as": "seller", "endpoint": "/api/a"},
                {"name": "s", "as": "seller", "endpoint": "/api/b"},
            ],
        }
        with pytest.raises(ValueError, match="Duplicate step names"):
            TestSetup.from_raw(raw)

    def test_rejects_unknown_default_viewer(self):
        raw = {
            "accounts": {"seller": {"email": "s@x"}},
            "default_viewer": "buyer",
        }
        with pytest.raises(ValueError, match="default_viewer 'buyer'"):
            TestSetup.from_raw(raw)

    def test_topological_order(self):
        raw = {
            "accounts": {"seller": {"email": "s@x"}, "buyer": {"email": "b@x"}},
            "default_viewer": "buyer",
            "steps": [
                {"name": "c", "as": "buyer", "endpoint": "/api/c", "depends_on": ["a", "b"]},
                {"name": "b", "as": "seller", "endpoint": "/api/b", "depends_on": ["a"]},
                {"name": "a", "as": "seller", "endpoint": "/api/a"},
            ],
        }
        ts = TestSetup.from_raw(raw)
        order = [s.name for s in ts.topological_order()]
        assert order == ["a", "b", "c"]


class TestTestSetupLegacyMigration:
    def test_migrates_seed_items_to_seed_role(self):
        raw = {
            "auth_endpoint": "/api/auth/login",
            "auth_payload": {"email": "${test_data.email}"},
            "seed_items": [
                {
                    "endpoint": "/api/listings",
                    "method": "POST",
                    "payload": {"title": "t"},
                    "id_path": "id",
                    "test_data_key": "listing_id",
                },
            ],
        }
        ts = TestSetup.from_raw(
            raw,
            seed_credentials={"email": "seed@x", "otp": "1111"},
            main_credentials={"email": "main@x", "otp": "2222"},
        )
        assert ts.auth_endpoint == "/api/auth/login"
        assert "seed" in ts.accounts
        assert ts.accounts["seed"].email == "seed@x"
        assert len(ts.steps) == 1
        step = ts.steps[0]
        assert step.name == "listing_id"
        assert step.as_role == "seed"
        assert step.save == {"listing_id": "id"}

    def test_migrates_take_item_to_main_role_with_dependency(self):
        raw = {
            "seed_items": [
                {
                    "endpoint": "/api/listings",
                    "payload": {"title": "t"},
                    "test_data_key": "listing_id",
                    "id_path": "id",
                },
            ],
            "take_item": {
                "endpoint": "/api/listings/${listing_id}/buy",
                "method": "POST",
                "test_data_key": "purchase_id",
                "id_path": "id",
            },
        }
        ts = TestSetup.from_raw(
            raw,
            seed_credentials={"email": "seed@x", "otp": "1"},
            main_credentials={"email": "main@x", "otp": "2"},
        )
        assert set(ts.accounts.keys()) == {"seed", "main"}
        assert ts.default_viewer == "main"
        names = [s.name for s in ts.steps]
        assert names == ["listing_id", "purchase_id"]
        assert ts.steps[0].as_role == "seed"
        assert ts.steps[1].as_role == "main"
        assert ts.steps[1].depends_on == ["listing_id"]

    def test_migrates_without_credentials_uses_stub_accounts(self):
        """If the user hasn't passed credentials, we still migrate and fill stubs.

        The runner will fail later if an empty stub is actually used, but
        validation must not block legacy configs that haven't been updated.
        """
        raw = {
            "seed_items": [{"endpoint": "/api/x", "test_data_key": "x_id", "id_path": "id"}],
        }
        ts = TestSetup.from_raw(raw)
        assert "seed" in ts.accounts
        assert ts.accounts["seed"].email is None

    def test_linear_chain_from_multiple_seed_items(self):
        raw = {
            "seed_items": [
                {"endpoint": "/api/a", "test_data_key": "a_id", "id_path": "id"},
                {"endpoint": "/api/b", "test_data_key": "b_id", "id_path": "id"},
                {"endpoint": "/api/c", "test_data_key": "c_id", "id_path": "id"},
            ],
        }
        ts = TestSetup.from_raw(raw, seed_credentials={"email": "s@x"})
        names = [s.name for s in ts.steps]
        assert names == ["a_id", "b_id", "c_id"]
        assert ts.steps[0].depends_on == []
        assert ts.steps[1].depends_on == ["a_id"]
        assert ts.steps[2].depends_on == ["b_id"]


class TestConfigTestSetupModel:
    def test_legacy_yaml_parses_via_accessor(self):
        data = {
            "test_credentials": {"email": "user@x", "otp": "1234"},
            "seed_account": {"email": "seed@x", "otp": "1234"},
            "test_setup": {
                "auth_endpoint": "/api/auth/login",
                "auth_payload": {"email": "${test_data.email}"},
                "seed_items": [
                    {
                        "endpoint": "/api/listings",
                        "payload": {"title": "t"},
                        "test_data_key": "listing_id",
                        "id_path": "id",
                    }
                ],
            },
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(data, f)
            path = Path(f.name)
        try:
            cfg = Config.load(config_path=path)
            ts = cfg.test_setup_model()
            assert ts.auth_endpoint == "/api/auth/login"
            assert ts.accounts["seed"].email == "seed@x"
            assert ts.default_viewer == "seed"  # no take_item → seed is the only viewer
            assert len(ts.steps) == 1
        finally:
            path.unlink()

    def test_new_shape_yaml_parses_via_accessor(self):
        data = {
            "test_setup": {
                "auth_endpoint": "/api/auth/login",
                "accounts": {
                    "seller": {"email": "seller@x", "otp": "1111"},
                    "buyer": {"email": "buyer@x", "otp": "2222"},
                },
                "default_viewer": "buyer",
                "steps": [
                    {
                        "name": "create_listing",
                        "as": "seller",
                        "endpoint": "/api/listings",
                        "payload": {"title": "Widget"},
                        "save": {"listing_id": "id"},
                    }
                ],
            }
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(data, f)
            path = Path(f.name)
        try:
            cfg = Config.load(config_path=path)
            ts = cfg.test_setup_model()
            assert set(ts.accounts.keys()) == {"seller", "buyer"}
            assert ts.default_viewer == "buyer"
            assert ts.steps[0].as_role == "seller"
            assert ts.steps[0].save == {"listing_id": "id"}
        finally:
            path.unlink()

    def test_empty_test_setup_returns_empty_model(self):
        cfg = Config()
        ts = cfg.test_setup_model()
        assert ts.accounts == {}
        assert ts.steps == []
