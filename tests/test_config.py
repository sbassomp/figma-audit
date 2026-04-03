"""Tests for configuration loading."""

import os
import tempfile
from pathlib import Path

import yaml

from figma_audit.config import Config


class TestConfigFileKey:
    def test_extract_from_design_url(self):
        cfg = Config(figma_url="https://www.figma.com/design/6kTFQMSueuk1dSDgiuMur9/MedCorp")
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
