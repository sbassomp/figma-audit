"""Tests for the pure helper functions in phases/analyze_code.py.

The full Phase 1 (one-shot or agentic) needs API access. These tests cover
only the framework-detection, file-discovery, and prompt-building helpers
that are pure and don't touch the network.
"""

from __future__ import annotations

from pathlib import Path

from figma_audit.phases.analyze_code import (
    API_PATTERNS,
    PAGE_PATTERNS,
    ROUTER_PATTERNS,
    TOKEN_PATTERNS,
    _build_prompt,
    _detect_framework,
    _find_files,
    _read_file_safe,
)


class TestDetectFramework:
    def test_flutter(self, tmp_path: Path) -> None:
        (tmp_path / "pubspec.yaml").write_text("name: my_app\n")
        assert _detect_framework(tmp_path) == "flutter"

    def test_nextjs(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text('{"dependencies": {"next": "14.0.0"}}')
        assert _detect_framework(tmp_path) == "nextjs"

    def test_nextjs_via_router(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text('{"dependencies": {"next/router": "14"}}')
        assert _detect_framework(tmp_path) == "nextjs"

    def test_vue(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text('{"dependencies": {"vue": "3.4"}}')
        assert _detect_framework(tmp_path) == "vue"

    def test_angular(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text('{"dependencies": {"@angular/core": "17"}}')
        assert _detect_framework(tmp_path) == "angular"

    def test_react(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text('{"dependencies": {"react": "18.2.0"}}')
        assert _detect_framework(tmp_path) == "react"

    def test_unknown(self, tmp_path: Path) -> None:
        (tmp_path / "README.md").write_text("hello")
        assert _detect_framework(tmp_path) == "unknown"

    def test_flutter_takes_precedence_over_package_json(self, tmp_path: Path) -> None:
        # Flutter projects can have both pubspec.yaml AND package.json (e.g. for tooling)
        # The function checks pubspec.yaml first, so flutter wins.
        (tmp_path / "pubspec.yaml").write_text("name: app\n")
        (tmp_path / "package.json").write_text('{"dependencies": {"react": "18"}}')
        assert _detect_framework(tmp_path) == "flutter"

    def test_nextjs_priority_over_react(self, tmp_path: Path) -> None:
        # next has react as a peer dependency — must match nextjs first
        (tmp_path / "package.json").write_text(
            '{"dependencies": {"react": "18", "next": "14"}}'
        )
        assert _detect_framework(tmp_path) == "nextjs"


class TestFindFiles:
    def test_finds_dart_files(self, tmp_path: Path) -> None:
        (tmp_path / "lib" / "pages").mkdir(parents=True)
        (tmp_path / "lib" / "pages" / "home.dart").write_text("class Home {}")
        (tmp_path / "lib" / "pages" / "login.dart").write_text("class Login {}")
        result = _find_files(tmp_path, ["**/pages/**/*.dart"])
        assert len(result) == 2
        assert all(f.suffix == ".dart" for f in result)

    def test_excludes_generated_files(self, tmp_path: Path) -> None:
        (tmp_path / "lib").mkdir()
        (tmp_path / "lib" / "model.dart").write_text("class Model {}")
        (tmp_path / "lib" / "model.g.dart").write_text("// generated")
        (tmp_path / "lib" / "model.freezed.dart").write_text("// generated")
        result = _find_files(tmp_path, ["**/*.dart"])
        names = [f.name for f in result]
        assert "model.dart" in names
        assert "model.g.dart" not in names
        assert "model.freezed.dart" not in names

    def test_excludes_build_dirs(self, tmp_path: Path) -> None:
        (tmp_path / "build" / "subdir").mkdir(parents=True)
        (tmp_path / "build" / "subdir" / "junk.dart").write_text("// build artifact")
        (tmp_path / "node_modules" / "pkg").mkdir(parents=True)
        (tmp_path / "node_modules" / "pkg" / "index.dart").write_text("// dep")
        (tmp_path / ".dart_tool").mkdir()
        (tmp_path / ".dart_tool" / "stuff.dart").write_text("// tool")
        (tmp_path / "lib").mkdir()
        (tmp_path / "lib" / "good.dart").write_text("class Good {}")
        result = _find_files(tmp_path, ["**/*.dart"])
        names = [f.name for f in result]
        assert "good.dart" in names
        assert "junk.dart" not in names
        assert "index.dart" not in names
        assert "stuff.dart" not in names

    def test_excludes_test_files(self, tmp_path: Path) -> None:
        (tmp_path / "lib").mkdir()
        (tmp_path / "lib" / "service.dart").write_text("class Service {}")
        (tmp_path / "test").mkdir()
        (tmp_path / "test" / "service_test.dart").write_text("// test")
        result = _find_files(tmp_path, ["**/*.dart"])
        names = [f.name for f in result]
        assert "service.dart" in names
        assert "service_test.dart" not in names

    def test_sorted_output(self, tmp_path: Path) -> None:
        (tmp_path / "lib").mkdir()
        for n in ["zebra.dart", "alpha.dart", "mango.dart"]:
            (tmp_path / "lib" / n).write_text("// file")
        result = _find_files(tmp_path, ["**/*.dart"])
        names = [f.name for f in result]
        assert names == sorted(names)

    def test_dedup_across_patterns(self, tmp_path: Path) -> None:
        """A file matching two glob patterns should appear only once."""
        (tmp_path / "lib" / "router").mkdir(parents=True)
        (tmp_path / "lib" / "router" / "app_router.dart").write_text("// router")
        result = _find_files(tmp_path, ["**/router/**/*.dart", "**/app_router.dart"])
        assert len(result) == 1


class TestReadFileSafe:
    def test_reads_normal_file(self, tmp_path: Path) -> None:
        path = tmp_path / "test.dart"
        path.write_text("class Test {}\n")
        content = _read_file_safe(path)
        assert content == "class Test {}\n"

    def test_truncates_large_file(self, tmp_path: Path) -> None:
        path = tmp_path / "big.dart"
        path.write_text("x" * 100_000)
        content = _read_file_safe(path, max_size=1000)
        assert content is not None
        # Content includes the truncation marker
        assert "truncated" in content
        # The actual data slice is exactly max_size chars
        assert content.startswith("x" * 1000)

    def test_returns_none_on_missing(self, tmp_path: Path) -> None:
        result = _read_file_safe(tmp_path / "nope.dart")
        assert result is None

    def test_returns_none_on_binary(self, tmp_path: Path) -> None:
        """Binary files should fail the UTF-8 decode and return None."""
        path = tmp_path / "binary.bin"
        path.write_bytes(b"\xff\xfe\x00\x01\x02\x03")
        result = _read_file_safe(path)
        assert result is None


class TestPatternConstants:
    def test_router_patterns_for_major_frameworks(self) -> None:
        assert "flutter" in ROUTER_PATTERNS
        assert "react" in ROUTER_PATTERNS
        assert any(".dart" in p for p in ROUTER_PATTERNS["flutter"])

    def test_page_patterns(self) -> None:
        assert "flutter" in PAGE_PATTERNS
        assert any("pages" in p for p in PAGE_PATTERNS["flutter"])

    def test_token_patterns(self) -> None:
        assert "flutter" in TOKEN_PATTERNS
        assert any("theme" in p for p in TOKEN_PATTERNS["flutter"])

    def test_api_patterns(self) -> None:
        assert "flutter" in API_PATTERNS
        assert any("repository" in p for p in API_PATTERNS["flutter"])


class TestBuildPrompt:
    def test_includes_framework_and_router_files(self, tmp_path: Path) -> None:
        prompt = _build_prompt(
            framework="flutter",
            router_files={"lib/router/app_router.dart": "class AppRouter {}"},
            page_files={},
            token_files={},
            api_files={},
            project_dir=tmp_path,
        )
        assert "flutter" in prompt
        assert "Router" in prompt
        assert "app_router.dart" in prompt
        assert "AppRouter" in prompt

    def test_includes_all_sections_when_present(self, tmp_path: Path) -> None:
        prompt = _build_prompt(
            framework="flutter",
            router_files={"router.dart": "// r"},
            page_files={"home.dart": "// h"},
            token_files={"theme.dart": "// t"},
            api_files={"api.dart": "// a"},
            project_dir=tmp_path,
        )
        assert "Router" in prompt
        assert "Page" in prompt
        assert "Token" in prompt
        assert "API" in prompt

    def test_omits_optional_sections_when_empty(self, tmp_path: Path) -> None:
        prompt = _build_prompt(
            framework="flutter",
            router_files={"router.dart": "// r"},
            page_files={},
            token_files={},
            api_files={},
            project_dir=tmp_path,
        )
        # With no API/token files, those sections should not appear
        assert "Token" not in prompt or "Token Files" not in prompt
        assert "API Client" not in prompt

    def test_caps_total_size(self, tmp_path: Path) -> None:
        # Build a prompt larger than the cap and verify it doesn't blow up
        big_content = "x" * 10_000
        page_files = {f"page_{i}.dart": big_content for i in range(50)}
        prompt = _build_prompt(
            framework="flutter",
            router_files={"router.dart": "// r"},
            page_files=page_files,
            token_files={},
            api_files={},
            project_dir=tmp_path,
        )
        # The function should have either truncated or omitted some files
        # (cap is 150K chars; 50 * 10K = 500K which exceeds it)
        assert len(prompt) < 250_000
