"""Tests for agent_tools sandbox enforcement and behavior."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from figma_audit.utils.agent_context import AgentContext
from figma_audit.utils.agent_tools import (
    GREP_CODE,
    LIST_FILES,
    READ_FILE,
    SUBMIT_RESULT,
    _redact_sensitive,
    _truncate,
    find_tool,
    format_tool_result,
    serialize_tools,
)

# ─── fixtures ────────────────────────────────────────────────────────

@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A small fake project tree for sandbox tests."""
    (tmp_path / "lib" / "auth").mkdir(parents=True)
    (tmp_path / "lib" / "auth" / "auth_repository.dart").write_text(
        "class AuthRepository {\n  Future<String> login() async { return 'token'; }\n}\n"
    )
    (tmp_path / "lib" / "models" / "course.dart").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "lib" / "models" / "course.dart").write_text(
        "class CreateCourseRequest {\n"
        "  required double departureLat;\n"
        "  required double departureLng;\n"
        "  required String departureAddress;\n"
        "}\n"
    )
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "junk.js").write_text("noise")
    (tmp_path / "secret.txt").write_text("token=hunter2")
    (tmp_path / "binary.bin").write_bytes(b"\x00\x01\x02\x03" * 100)
    return tmp_path


@pytest.fixture
def ctx(project: Path) -> AgentContext:
    return AgentContext(project_dir=project, interactive=False)


# ─── read_file ───────────────────────────────────────────────────────

class TestReadFile:
    def test_reads_existing_file(self, ctx: AgentContext) -> None:
        result = READ_FILE.run({"path": "lib/auth/auth_repository.dart"}, ctx)
        assert "error" not in result
        assert "AuthRepository" in result["content"]
        assert result["truncated"] is False

    def test_rejects_absolute_path(self, ctx: AgentContext) -> None:
        result = READ_FILE.run({"path": "/etc/passwd"}, ctx)
        assert "error" in result
        assert "invalid path" in result["error"]

    def test_rejects_parent_traversal(self, ctx: AgentContext) -> None:
        result = READ_FILE.run({"path": "../../etc/passwd"}, ctx)
        assert "error" in result

    def test_rejects_symlink_escape(self, ctx: AgentContext, tmp_path: Path) -> None:
        outside = tmp_path.parent / "outside_target.txt"
        outside.write_text("secret")
        link = ctx.project_dir / "evil_link"
        try:
            os.symlink(outside, link)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not supported on this platform")
        result = READ_FILE.run({"path": "evil_link"}, ctx)
        assert "error" in result
        assert "escapes sandbox" in result["error"]

    def test_rejects_binary_file(self, ctx: AgentContext) -> None:
        result = READ_FILE.run({"path": "binary.bin"}, ctx)
        assert "error" in result
        assert "binary file refused" in result["error"]

    def test_rejects_missing_file(self, ctx: AgentContext) -> None:
        result = READ_FILE.run({"path": "lib/does_not_exist.dart"}, ctx)
        assert "error" in result
        assert "not found" in result["error"]

    def test_rejects_directory(self, ctx: AgentContext) -> None:
        result = READ_FILE.run({"path": "lib"}, ctx)
        assert "error" in result
        assert "not a regular file" in result["error"]

    def test_truncates_large_files(self, ctx: AgentContext, project: Path) -> None:
        big = project / "big.txt"
        big.write_text("a" * 100_000)
        result = READ_FILE.run({"path": "big.txt", "max_bytes": 1000}, ctx)
        assert result["truncated"] is True
        assert len(result["content"]) == 1000

    def test_offset_works(self, ctx: AgentContext, project: Path) -> None:
        target = project / "offset_test.txt"
        target.write_text("0123456789ABCDEF")
        result = READ_FILE.run({"path": "offset_test.txt", "offset": 4}, ctx)
        assert result["content"] == "456789ABCDEF"

    def test_max_bytes_capped_to_context_limit(self, project: Path) -> None:
        ctx = AgentContext(project_dir=project, interactive=False, max_file_bytes=512)
        target = project / "small.txt"
        target.write_text("x" * 2000)
        result = READ_FILE.run({"path": "small.txt", "max_bytes": 99999}, ctx)
        # Cap should win
        assert len(result["content"]) == 512


# ─── grep_code ───────────────────────────────────────────────────────

class TestGrepCode:
    def test_finds_pattern(self, ctx: AgentContext) -> None:
        result = GREP_CODE.run({"pattern": "AuthRepository"}, ctx)
        assert "error" not in result
        assert result["match_count"] >= 1
        assert any("auth_repository.dart" in m for m in result["matches"])

    def test_excludes_node_modules(self, ctx: AgentContext) -> None:
        result = GREP_CODE.run({"pattern": "noise"}, ctx)
        # Either the python fallback excludes it, or rg with default ignores
        # respects .gitignore-style. Either way, no node_modules hits.
        assert all("node_modules" not in m for m in result["matches"])

    def test_glob_filter(self, ctx: AgentContext) -> None:
        result = GREP_CODE.run(
            {"pattern": "departure", "file_glob": "**/*.dart"}, ctx
        )
        assert result["match_count"] >= 1
        assert all(m.endswith(".dart") or ".dart:" in m for m in result["matches"])

    def test_rejects_absolute_glob(self, ctx: AgentContext) -> None:
        result = GREP_CODE.run({"pattern": "x", "file_glob": "/etc/*"}, ctx)
        assert "error" in result

    def test_rejects_traversal_glob(self, ctx: AgentContext) -> None:
        result = GREP_CODE.run({"pattern": "x", "file_glob": "../*.txt"}, ctx)
        assert "error" in result

    def test_empty_pattern_rejected(self, ctx: AgentContext) -> None:
        result = GREP_CODE.run({"pattern": ""}, ctx)
        assert "error" in result

    def test_max_results_capped(self, ctx: AgentContext, project: Path) -> None:
        # Create many matches
        big = project / "many.txt"
        big.write_text("\n".join(f"line {i}: target" for i in range(500)))
        result = GREP_CODE.run({"pattern": "target", "max_results": 50}, ctx)
        assert result["match_count"] <= 50


# ─── list_files ──────────────────────────────────────────────────────

class TestListFiles:
    def test_lists_root(self, ctx: AgentContext) -> None:
        result = LIST_FILES.run({"directory": "."}, ctx)
        assert "error" not in result
        names = [e["path"] for e in result["entries"]]
        assert "lib" in names

    def test_excludes_node_modules(self, ctx: AgentContext) -> None:
        result = LIST_FILES.run({"directory": ".", "recursive": True}, ctx)
        assert all("node_modules" not in e["path"] for e in result["entries"])

    def test_recursive(self, ctx: AgentContext) -> None:
        result = LIST_FILES.run({"directory": "lib", "recursive": True}, ctx)
        paths = [e["path"] for e in result["entries"]]
        assert any("auth_repository.dart" in p for p in paths)
        assert any("course.dart" in p for p in paths)

    def test_rejects_traversal(self, ctx: AgentContext) -> None:
        result = LIST_FILES.run({"directory": "../"}, ctx)
        assert "error" in result

    def test_rejects_absolute(self, ctx: AgentContext) -> None:
        result = LIST_FILES.run({"directory": "/tmp"}, ctx)
        assert "error" in result

    def test_max_entries(self, ctx: AgentContext, project: Path) -> None:
        for i in range(50):
            (project / f"file_{i}.txt").write_text("x")
        result = LIST_FILES.run({"directory": ".", "max_entries": 10}, ctx)
        assert result["entry_count"] <= 10
        assert result["capped"] is True


# ─── helpers ─────────────────────────────────────────────────────────

class TestHelpers:
    def test_redact_sensitive(self) -> None:
        body = {
            "user": "alice",
            "password": "hunter2",
            "tokens": {"accessToken": "abc", "refreshToken": "def"},
            "list": [{"api_key": "k1"}, {"safe": "v"}],
        }
        out = _redact_sensitive(body)
        assert out["user"] == "alice"
        assert out["password"] == "<redacted>"
        # The "tokens" key matches "token", so the entire subtree is redacted
        assert out["tokens"] == "<redacted>"
        assert out["list"][0]["api_key"] == "<redacted>"
        assert out["list"][1]["safe"] == "v"

    def test_truncate(self) -> None:
        small = "hello"
        assert _truncate(small) == "hello"
        big = "x" * 30_000
        result = _truncate(big)
        assert len(result) <= 20_000
        assert "truncated" in result

    def test_format_tool_result_string(self) -> None:
        assert format_tool_result("hi") == "hi"

    def test_format_tool_result_dict(self) -> None:
        result = format_tool_result({"a": 1})
        assert '"a": 1' in result or '"a":1' in result

    def test_serialize_tools_shape(self) -> None:
        serialized = serialize_tools([READ_FILE, SUBMIT_RESULT])
        assert len(serialized) == 2
        assert all("name" in t and "description" in t and "input_schema" in t
                   for t in serialized)

    def test_find_tool(self) -> None:
        from figma_audit.utils.agent_tools import READONLY_TOOLS
        assert find_tool(READONLY_TOOLS, "read_file") is READ_FILE
        assert find_tool(READONLY_TOOLS, "nonexistent") is None
