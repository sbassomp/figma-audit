"""Tests for http_request and ask_user agent tools.

Uses a stdlib http.server in a background thread instead of pulling in
pytest-httpserver as a new dependency.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from figma_audit.utils.agent_context import AgentContext
from figma_audit.utils.agent_tools import ASK_USER, HTTP_REQUEST

# ─── tiny test server ────────────────────────────────────────────────


class _TestHandler(BaseHTTPRequestHandler):
    """Programmable handler — reads `RESPONSES` set on the server instance."""

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        pass  # silence

    def _send_json(self, status: int, body: dict) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _handle(self) -> None:
        server: _TestServer = self.server  # type: ignore[assignment]
        server.received.append(
            {
                "method": self.command,
                "path": self.path,
                "headers": dict(self.headers),
                "body": self._read_body(),
            }
        )
        key = (self.command, self.path)
        if key in server.responses:
            status, body = server.responses[key]
            self._send_json(status, body)
        else:
            self._send_json(404, {"error": "not configured"})

    def _read_body(self) -> dict | None:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if not length:
            return None
        try:
            return json.loads(self.rfile.read(length).decode())
        except (ValueError, UnicodeDecodeError):
            return None

    def do_GET(self) -> None:  # noqa: N802
        self._handle()

    def do_POST(self) -> None:  # noqa: N802
        self._handle()

    def do_PUT(self) -> None:  # noqa: N802
        self._handle()

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle()


class _TestServer(HTTPServer):
    responses: dict
    received: list


@pytest.fixture
def http_server():
    server = _TestServer(("127.0.0.1", 0), _TestHandler)
    server.responses = {}
    server.received = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def base_url(http_server) -> str:
    host, port = http_server.server_address
    return f"http://{host}:{port}"


@pytest.fixture
def ctx(tmp_path: Path, base_url: str) -> AgentContext:
    return AgentContext(
        project_dir=tmp_path,
        app_url=base_url,
        auth_token="test-token-123",
        interactive=False,
    )


# ─── http_request ────────────────────────────────────────────────────


class TestHttpRequest:
    def test_basic_get(self, http_server, ctx: AgentContext) -> None:
        http_server.responses[("GET", "/api/health")] = (200, {"status": "ok"})
        result = HTTP_REQUEST.run({"method": "GET", "path": "/api/health"}, ctx)
        assert result["status"] == 200
        assert result["body"] == {"status": "ok"}

    def test_post_with_body(self, http_server, ctx: AgentContext) -> None:
        http_server.responses[("POST", "/api/items")] = (201, {"id": "abc-123"})
        result = HTTP_REQUEST.run(
            {
                "method": "POST",
                "path": "/api/items",
                "body": {"name": "test"},
            },
            ctx,
        )
        assert result["status"] == 201
        assert result["body"] == {"id": "abc-123"}
        # Server should have received the body
        assert http_server.received[0]["body"] == {"name": "test"}

    def test_auth_header_injected(self, http_server, ctx: AgentContext) -> None:
        http_server.responses[("GET", "/api/me")] = (200, {"user": "alice"})
        HTTP_REQUEST.run({"method": "GET", "path": "/api/me"}, ctx)
        assert http_server.received[0]["headers"]["Authorization"] == "Bearer test-token-123"

    def test_auth_disabled(self, http_server, ctx: AgentContext) -> None:
        http_server.responses[("GET", "/api/public")] = (200, {})
        HTTP_REQUEST.run({"method": "GET", "path": "/api/public", "use_auth": False}, ctx)
        assert "Authorization" not in http_server.received[0]["headers"]

    def test_400_response_returned(self, http_server, ctx: AgentContext) -> None:
        http_server.responses[("POST", "/api/items")] = (
            400,
            {"detail": "field 'name' is required"},
        )
        result = HTTP_REQUEST.run({"method": "POST", "path": "/api/items", "body": {}}, ctx)
        assert result["status"] == 400
        assert "field 'name'" in str(result["body"])

    def test_redacts_sensitive_in_response_body(self, http_server, ctx: AgentContext) -> None:
        http_server.responses[("POST", "/api/login")] = (
            200,
            {"user": "alice", "accessToken": "SECRET123"},
        )
        result = HTTP_REQUEST.run({"method": "POST", "path": "/api/login", "body": {}}, ctx)
        # accessToken matches the "token" pattern → redacted
        assert result["body"]["accessToken"] == "<redacted>"

    def test_rejects_absolute_url(self, ctx: AgentContext) -> None:
        result = HTTP_REQUEST.run({"method": "GET", "path": "https://evil.com/steal"}, ctx)
        assert "error" in result

    def test_rejects_traversal(self, ctx: AgentContext) -> None:
        result = HTTP_REQUEST.run({"method": "GET", "path": "/api/../../../etc/passwd"}, ctx)
        assert "error" in result

    def test_rejects_when_no_app_url(self, tmp_path: Path) -> None:
        ctx = AgentContext(project_dir=tmp_path, app_url=None, interactive=False)
        result = HTTP_REQUEST.run({"method": "GET", "path": "/api/x"}, ctx)
        assert "error" in result
        assert "no app_url" in result["error"]

    def test_anti_loop_blocks_third_identical(self, http_server, ctx: AgentContext) -> None:
        http_server.responses[("POST", "/api/items")] = (
            400,
            {"error": "still wrong"},
        )
        # First two should go through to the server
        for _ in range(2):
            result = HTTP_REQUEST.run(
                {"method": "POST", "path": "/api/items", "body": {"x": 1}}, ctx
            )
            assert result["status"] == 400
        # Third must be refused before hitting the network
        before_count = len(http_server.received)
        result = HTTP_REQUEST.run({"method": "POST", "path": "/api/items", "body": {"x": 1}}, ctx)
        assert result["status"] == 0
        assert "REFUSED" in result["error"]
        assert len(http_server.received) == before_count  # no new server hit

    def test_different_payload_not_blocked(self, http_server, ctx: AgentContext) -> None:
        http_server.responses[("POST", "/api/items")] = (400, {"error": "x"})
        for x in range(5):
            HTTP_REQUEST.run({"method": "POST", "path": "/api/items", "body": {"x": x}}, ctx)
        # All 5 should hit the server because the body is different each time
        assert len(http_server.received) == 5

    def test_invalid_method(self, ctx: AgentContext) -> None:
        result = HTTP_REQUEST.run({"method": "TRACE", "path": "/api/x"}, ctx)
        assert "error" in result

    def test_multi_token_default_role(self, http_server, tmp_path: Path, base_url: str) -> None:
        """When no ``as`` is passed, the default role's token is used."""
        ctx = AgentContext(
            project_dir=tmp_path,
            app_url=base_url,
            tokens={"seller": "seller-token", "buyer": "buyer-token"},
            default_role="seller",
            interactive=False,
        )
        http_server.responses[("GET", "/api/me")] = (200, {"who": "?"})
        HTTP_REQUEST.run({"method": "GET", "path": "/api/me"}, ctx)
        assert http_server.received[0]["headers"]["Authorization"] == "Bearer seller-token"

    def test_multi_token_explicit_role(self, http_server, tmp_path: Path, base_url: str) -> None:
        """Passing ``as`` selects the matching role's token."""
        ctx = AgentContext(
            project_dir=tmp_path,
            app_url=base_url,
            tokens={"seller": "seller-token", "buyer": "buyer-token"},
            default_role="seller",
            interactive=False,
        )
        http_server.responses[("POST", "/api/orders")] = (201, {"id": "o-1"})
        result = HTTP_REQUEST.run(
            {"method": "POST", "path": "/api/orders", "body": {"x": 1}, "as": "buyer"},
            ctx,
        )
        assert http_server.received[0]["headers"]["Authorization"] == "Bearer buyer-token"
        assert result["as"] == "buyer"

    def test_multi_token_unknown_role_rejected(
        self, http_server, tmp_path: Path, base_url: str
    ) -> None:
        ctx = AgentContext(
            project_dir=tmp_path,
            app_url=base_url,
            tokens={"seller": "seller-token"},
            default_role="seller",
            interactive=False,
        )
        result = HTTP_REQUEST.run(
            {"method": "GET", "path": "/api/x", "as": "ghost"},
            ctx,
        )
        assert "error" in result
        assert "ghost" in result["error"]
        # Server must not have been hit
        assert len(http_server.received) == 0

    def test_legacy_auth_token_still_works(
        self, http_server, tmp_path: Path, base_url: str
    ) -> None:
        """``auth_token=...`` shortcut still populates the default token slot."""
        ctx = AgentContext(
            project_dir=tmp_path,
            app_url=base_url,
            auth_token="legacy-token",
            interactive=False,
        )
        assert ctx.tokens == {"default": "legacy-token"}
        assert ctx.default_role == "default"
        http_server.responses[("GET", "/api/me")] = (200, {})
        HTTP_REQUEST.run({"method": "GET", "path": "/api/me"}, ctx)
        assert http_server.received[0]["headers"]["Authorization"] == "Bearer legacy-token"

    def test_same_payload_different_role_not_deduped(
        self, http_server, tmp_path: Path, base_url: str
    ) -> None:
        """Anti-loop is role-scoped: the same POST as seller then buyer is valid."""
        ctx = AgentContext(
            project_dir=tmp_path,
            app_url=base_url,
            tokens={"seller": "s", "buyer": "b"},
            default_role="seller",
            interactive=False,
        )
        http_server.responses[("POST", "/api/items")] = (201, {"id": "i"})
        for role in ("seller", "buyer", "seller", "buyer"):
            HTTP_REQUEST.run(
                {"method": "POST", "path": "/api/items", "body": {"x": 1}, "as": role},
                ctx,
            )
        assert len(http_server.received) == 4

    def test_budget_cap(self, http_server, tmp_path: Path, base_url: str) -> None:
        ctx = AgentContext(
            project_dir=tmp_path,
            app_url=base_url,
            interactive=False,
            max_http_calls=3,
        )
        http_server.responses[("GET", "/api/x")] = (200, {})
        # Vary path to defeat the anti-loop dedup
        for i in range(3):
            http_server.responses[("GET", f"/api/x{i}")] = (200, {})
            result = HTTP_REQUEST.run({"method": "GET", "path": f"/api/x{i}"}, ctx)
            assert result["status"] == 200
        # 4th call exceeds the budget
        result = HTTP_REQUEST.run({"method": "GET", "path": "/api/x99"}, ctx)
        assert "error" in result
        assert "budget exceeded" in result["error"]


# ─── ask_user ────────────────────────────────────────────────────────


class TestAskUser:
    def test_non_interactive_returns_no_answer(self, tmp_path: Path) -> None:
        ctx = AgentContext(project_dir=tmp_path, interactive=False)
        result = ASK_USER.run({"question": "what now?"}, ctx)
        assert result["answer"] is None
        assert "non-interactive" in result["note"]

    def test_interactive_with_prompt(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        ctx = AgentContext(project_dir=tmp_path, interactive=True)
        import click as _click

        monkeypatch.setattr(_click, "prompt", lambda *a, **k: "yes")
        result = ASK_USER.run({"question": "do the thing?"}, ctx)
        assert result["answer"] == "yes"

    def test_choices_numeric_resolution(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = AgentContext(project_dir=tmp_path, interactive=True)
        import click as _click

        monkeypatch.setattr(_click, "prompt", lambda *a, **k: "2")
        result = ASK_USER.run({"question": "which?", "choices": ["alpha", "beta", "gamma"]}, ctx)
        assert result["answer"] == "beta"

    def test_anti_begging(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        ctx = AgentContext(project_dir=tmp_path, interactive=True)
        import click as _click

        monkeypatch.setattr(_click, "prompt", lambda *a, **k: "yes")
        # First time: works
        result1 = ASK_USER.run({"question": "same q"}, ctx)
        assert result1["answer"] == "yes"
        # Second time same question: refused
        result2 = ASK_USER.run({"question": "same q"}, ctx)
        assert result2["answer"] is None
        assert "already asked" in result2["note"]

    def test_empty_question_rejected(self, tmp_path: Path) -> None:
        ctx = AgentContext(project_dir=tmp_path, interactive=False)
        result = ASK_USER.run({"question": ""}, ctx)
        assert "error" in result
