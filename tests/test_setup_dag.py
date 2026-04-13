"""Tests for the Phase 4 multi-actor seed DAG executor.

Exercises :func:`_run_setup_dag` end-to-end against an in-process HTTP
server, verifying that:

- steps run in topological order
- the correct bearer token is used per role
- ``save`` values are extracted from responses and injected into
  ``test_data`` for use by later steps
- missing tokens cause graceful skips (no crash)
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from figma_audit.config import Account, SetupStep, TestSetup
from figma_audit.phases.capture_app.api_client import _run_setup_dag


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:  # noqa: A002
        pass

    def _send(self, status: int, body: dict) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _handle(self) -> None:
        srv: _Server = self.server  # type: ignore[assignment]
        length = int(self.headers.get("Content-Length", 0) or 0)
        try:
            body = json.loads(self.rfile.read(length).decode()) if length else None
        except (ValueError, UnicodeDecodeError):
            body = None
        srv.received.append(
            {
                "method": self.command,
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "body": body,
            }
        )
        key = (self.command, self.path)
        if key in srv.responses:
            status, resp = srv.responses[key]
            self._send(status, resp)
        else:
            self._send(404, {"error": "not configured"})

    def do_GET(self) -> None:  # noqa: N802
        self._handle()

    def do_POST(self) -> None:  # noqa: N802
        self._handle()


class _Server(HTTPServer):
    responses: dict
    received: list


@pytest.fixture
def server():
    s = _Server(("127.0.0.1", 0), _Handler)
    s.responses = {}
    s.received = []
    t = threading.Thread(target=s.serve_forever, daemon=True)
    t.start()
    yield s
    s.shutdown()
    s.server_close()


@pytest.fixture
def base_url(server) -> str:
    host, port = server.server_address
    return f"http://{host}:{port}"


class TestRunSetupDag:
    def test_two_actor_flow_with_dependency(self, server: _Server, base_url: str) -> None:
        """seller creates listing → buyer places order referencing the saved id."""
        server.responses[("POST", "/api/listings")] = (201, {"id": "listing-42"})
        server.responses[("POST", "/api/listings/listing-42/orders")] = (
            201,
            {"id": "order-7"},
        )

        setup = TestSetup(
            accounts={
                "seller": Account(email="seller@x"),
                "buyer": Account(email="buyer@x"),
            },
            default_viewer="buyer",
            steps=[
                SetupStep(
                    name="create_listing",
                    as_role="seller",
                    endpoint="/api/listings",
                    method="POST",
                    payload={"title": "Widget"},
                    save={"listing_id": "id"},
                ),
                SetupStep(
                    name="place_order",
                    as_role="buyer",
                    endpoint="/api/listings/${listing_id}/orders",
                    method="POST",
                    payload={"quantity": 1},
                    save={"order_id": "id"},
                    depends_on=["create_listing"],
                ),
            ],
        )
        tokens = {"seller": "seller-tok", "buyer": "buyer-tok"}
        test_data: dict = {}

        completed = _run_setup_dag(base_url, setup, tokens, test_data, {})

        assert completed == ["create_listing", "place_order"]
        assert test_data["listing_id"] == "listing-42"
        assert test_data["order_id"] == "order-7"
        # Seller token used for step 1, buyer token for step 2.
        assert server.received[0]["authorization"] == "Bearer seller-tok"
        assert server.received[1]["authorization"] == "Bearer buyer-tok"
        # Second URL has the saved listing_id templated in.
        assert server.received[1]["path"] == "/api/listings/listing-42/orders"

    def test_step_skipped_when_role_has_no_token(self, server: _Server, base_url: str) -> None:
        """A step tagged with a role that failed to auth is skipped, not failed."""
        server.responses[("POST", "/api/listings")] = (201, {"id": "l-1"})
        setup = TestSetup(
            accounts={
                "seller": Account(email="seller@x"),
                "ghost": Account(email="ghost@x"),
            },
            default_viewer="seller",
            steps=[
                SetupStep(
                    name="create_listing",
                    as_role="seller",
                    endpoint="/api/listings",
                    method="POST",
                    save={"listing_id": "id"},
                ),
                SetupStep(
                    name="ghost_action",
                    as_role="ghost",  # no token registered
                    endpoint="/api/ghost",
                    method="POST",
                    depends_on=["create_listing"],
                ),
            ],
        )
        tokens = {"seller": "s-tok"}  # ghost intentionally missing
        test_data: dict = {}

        completed = _run_setup_dag(base_url, setup, tokens, test_data, {})

        assert completed == ["create_listing"]
        assert len(server.received) == 1  # ghost step never hit the network

    def test_http_error_does_not_crash(self, server: _Server, base_url: str) -> None:
        """A 400 from the backend is logged and the step is skipped."""
        server.responses[("POST", "/api/broken")] = (400, {"error": "bad"})
        setup = TestSetup(
            accounts={"user": Account(email="u@x")},
            default_viewer="user",
            steps=[
                SetupStep(
                    name="broken_step",
                    as_role="user",
                    endpoint="/api/broken",
                    method="POST",
                    save={"some_id": "id"},
                ),
            ],
        )
        completed = _run_setup_dag(base_url, setup, {"user": "tok"}, {}, {})
        assert completed == []

    def test_empty_dag_returns_empty(self) -> None:
        setup = TestSetup(accounts={"u": Account(email="u@x")}, default_viewer="u")
        assert _run_setup_dag("http://unused", setup, {"u": "t"}, {}, {}) == []
