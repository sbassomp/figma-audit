"""Tests for ``_extract_jwt_sub``.

We hand-build JWT-shaped strings (header.payload.signature with base64url
payloads) so we can verify parsing without pulling in a JWT library.
"""

from __future__ import annotations

import base64
import json

from figma_audit.phases.capture_app.api_client import _extract_jwt_sub


def _make_jwt(payload: dict) -> str:
    """Return a dotted JWT-shaped string whose middle segment encodes payload.

    Header and signature are filler values — ``_extract_jwt_sub`` only
    reads the middle segment and never verifies the signature.
    """
    header_b64 = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload_json = json.dumps(payload).encode()
    payload_b64 = base64.urlsafe_b64encode(payload_json).rstrip(b"=").decode()
    return f"{header_b64}.{payload_b64}.signature"


class TestExtractJwtSub:
    def test_returns_sub_claim(self):
        token = _make_jwt({"sub": "52cc28a6-1b6f-4bd8-8b7b-c09d4d6c8489", "role": "driver"})
        assert _extract_jwt_sub(token) == "52cc28a6-1b6f-4bd8-8b7b-c09d4d6c8489"

    def test_returns_none_when_sub_missing(self):
        token = _make_jwt({"email": "x@y.com"})
        assert _extract_jwt_sub(token) is None

    def test_returns_none_on_empty_input(self):
        assert _extract_jwt_sub("") is None
        assert _extract_jwt_sub(None) is None  # type: ignore[arg-type]

    def test_returns_none_on_invalid_base64(self):
        assert _extract_jwt_sub("not.avalidjwt.really") is None

    def test_returns_none_on_missing_dots(self):
        assert _extract_jwt_sub("no-dots-here") is None

    def test_handles_unpadded_base64(self):
        """Real JWTs omit the base64 padding; the helper must re-pad."""
        payload = {"sub": "abc"}
        # Build a JWT whose middle segment has an unpadded length
        # (base64 of 6 bytes needs no padding; base64 of 7 bytes needs 2).
        json_bytes = json.dumps(payload).encode()
        while len(json_bytes) % 3 == 0:
            json_bytes += b" "  # force padding difference
        payload_b64 = base64.urlsafe_b64encode(json_bytes).rstrip(b"=").decode()
        token = f"header.{payload_b64}.sig"
        assert _extract_jwt_sub(token) == "abc"

    def test_sub_coerced_to_string(self):
        """Some tokens carry a numeric sub. We always return a string."""
        token = _make_jwt({"sub": 42})
        assert _extract_jwt_sub(token) == "42"
