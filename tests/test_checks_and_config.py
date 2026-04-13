"""Tests for utils/checks.py and config loading edge cases."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from figma_audit.config import Config
from figma_audit.utils.checks import (
    check_api_keys,
    check_playwright_browser,
    load_env_file,
)


class TestCheckApiKeys:
    def test_returns_true_when_env_var_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-xxx")
        assert check_api_keys() is True

    def test_returns_false_when_unset(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        # Point HOME to an empty dir so the config file doesn't exist
        monkeypatch.setenv("HOME", str(tmp_path))
        assert check_api_keys() is False

    def test_reads_from_config_file(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        cfg_dir = tmp_path / ".config" / "figma-audit"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "env").write_text("ANTHROPIC_API_KEY=sk-ant-fromfile-12345\n")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        assert check_api_keys() is True


class TestLoadEnvFile:
    def test_loads_keys_into_environ(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("FIGMA_TOKEN", raising=False)
        cfg_dir = tmp_path / ".config" / "figma-audit"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "env").write_text("ANTHROPIC_API_KEY=sk-ant-test\nFIGMA_TOKEN=figd_test\n")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        load_env_file()
        assert os.environ.get("ANTHROPIC_API_KEY") == "sk-ant-test"
        assert os.environ.get("FIGMA_TOKEN") == "figd_test"

    def test_does_not_overwrite_existing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "from-env")
        cfg_dir = tmp_path / ".config" / "figma-audit"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "env").write_text("ANTHROPIC_API_KEY=from-file\n")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        load_env_file()
        assert os.environ["ANTHROPIC_API_KEY"] == "from-env"

    def test_handles_missing_file(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        # Should not raise even if the config dir doesn't exist
        load_env_file()

    def test_skips_comments_and_blank_lines(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        cfg_dir = tmp_path / ".config" / "figma-audit"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "env").write_text(
            "# This is a comment\n\nANTHROPIC_API_KEY=valid-key\n# Another comment\n"
        )
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        load_env_file()
        assert os.environ["ANTHROPIC_API_KEY"] == "valid-key"


class TestCheckPlaywrightBrowser:
    def test_returns_true_when_chromium_installed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        cache = tmp_path / ".cache" / "ms-playwright" / "chromium-1234"
        cache.mkdir(parents=True)
        (cache / "INSTALLATION_COMPLETE").touch()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        assert check_playwright_browser() is True

    def test_returns_false_when_no_cache_dir(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        assert check_playwright_browser() is False

    def test_returns_false_when_marker_missing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        cache = tmp_path / ".cache" / "ms-playwright" / "chromium-1234"
        cache.mkdir(parents=True)
        # No INSTALLATION_COMPLETE marker
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        assert check_playwright_browser() is False


class TestConfigLoad:
    def test_default_values(self) -> None:
        cfg = Config()
        assert cfg.output == "./audit-results"
        assert cfg.viewport.width == 390
        assert cfg.viewport.height == 844
        assert cfg.analyze_mode == "one-shot"
        assert cfg.analyze_model is None

    def test_load_from_yaml(self, tmp_path: Path) -> None:
        yaml_file = tmp_path / "config.yaml"
        yaml_file.write_text(
            "project: ~/dev/myapp\n"
            "app_url: https://example.com\n"
            "output: ./out\n"
            "analyze_mode: agentic\n"
        )
        cfg = Config.load(config_path=yaml_file)
        assert cfg.project == "~/dev/myapp"
        assert cfg.app_url == "https://example.com"
        assert cfg.output == "./out"
        assert cfg.analyze_mode == "agentic"

    def test_cli_overrides_yaml(self, tmp_path: Path) -> None:
        yaml_file = tmp_path / "config.yaml"
        yaml_file.write_text("project: ~/dev/from-yaml\nfigma_url: https://from.yaml\n")
        cfg = Config.load(
            config_path=yaml_file,
            project="~/dev/from-cli",
        )
        assert cfg.project == "~/dev/from-cli"
        # figma_url not overridden, kept from yaml
        assert cfg.figma_url == "https://from.yaml"

    def test_env_var_fallback(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-env")
        monkeypatch.setenv("FIGMA_TOKEN", "figd_from_env")
        cfg = Config.load()
        assert cfg.anthropic_api_key == "sk-ant-from-env"
        assert cfg.figma_token == "figd_from_env"

    def test_output_dir_resolves_to_path(self) -> None:
        cfg = Config(output="./somewhere")
        assert isinstance(cfg.output_dir, Path)
        assert cfg.output_dir.is_absolute()

    def test_figma_file_key_from_url(self) -> None:
        cfg = Config(figma_url="https://www.figma.com/design/ABC123/MyProject")
        assert cfg.figma_file_key == "ABC123"

    def test_figma_file_key_from_file_path(self) -> None:
        cfg = Config(figma_file="/tmp/MyProject.fig")
        assert cfg.figma_file_key == "MyProject"

    def test_figma_file_key_none_when_unset(self) -> None:
        cfg = Config()
        assert cfg.figma_file_key is None

    def test_figma_file_path_resolves(self, tmp_path: Path) -> None:
        cfg = Config(figma_file=str(tmp_path / "test.fig"))
        path = cfg.figma_file_path
        assert path is not None
        assert path.is_absolute()
