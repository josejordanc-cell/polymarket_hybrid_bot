"""tests/api/test_auth.py — Unit tests for api/auth.py.

Covers:
    verify_bearer  — 9 tests (TestVerifyBearer)
    verify_ws_ticket — 10 tests (TestVerifyWsTicket)

Design notes:
- Plain dict is sufficient for headers; verify_bearer uses .get().
- WS tests use _make_ticket() with a UUID-fresh sub per call to avoid
  collisions with api.auth._consumed_subs across test runs.
- Missing-config tests build an inline MagicMock; the bot_config fixture
  is never mutated.
"""
from __future__ import annotations

import time
import uuid
from typing import Any
from unittest.mock import MagicMock

import jwt
import pytest

from api.auth import verify_bearer, verify_ws_ticket


# ---------------------------------------------------------------------------
# Module-level helper
# ---------------------------------------------------------------------------

def _make_ticket(
    secret: str,
    *,
    sub: str | None = None,
    iat: int | None = None,
    exp: int | None = None,
    algorithm: str = "HS256",
    include_sub: bool = True,
    include_iat: bool = True,
) -> str:
    """Build a JWT for verify_ws_ticket tests.

    sub defaults to a fresh UUID per call so module-level _consumed_subs
    state in api.auth never collides across tests.
    """
    now = int(time.time())
    payload: dict[str, Any] = {}
    if include_sub:
        payload["sub"] = sub if sub is not None else f"test-sub-{uuid.uuid4()}"
    if include_iat:
        payload["iat"] = iat if iat is not None else now
    payload["exp"] = exp if exp is not None else now + 60
    return jwt.encode(payload, secret, algorithm=algorithm)


# ---------------------------------------------------------------------------
# TestVerifyBearer — 9 tests
# ---------------------------------------------------------------------------

class TestVerifyBearer:
    """Tests for verify_bearer(request_headers, config)."""

    def test_valid_bearer(self, bot_config: MagicMock) -> None:
        headers = {"Authorization": "Bearer test-bearer-token-abc123"}
        assert verify_bearer(headers, bot_config) is True

    def test_missing_header(self, bot_config: MagicMock) -> None:
        headers: dict[str, str] = {}
        assert verify_bearer(headers, bot_config) is False

    def test_empty_auth_value(self, bot_config: MagicMock) -> None:
        headers = {"Authorization": ""}
        assert verify_bearer(headers, bot_config) is False

    def test_malformed_no_token(self, bot_config: MagicMock) -> None:
        # "Bearer" with no trailing token — split gives one part only.
        headers = {"Authorization": "Bearer"}
        assert verify_bearer(headers, bot_config) is False

    def test_malformed_single_word(self, bot_config: MagicMock) -> None:
        # Single word, no scheme separator.
        headers = {"Authorization": "onlyoneword"}
        assert verify_bearer(headers, bot_config) is False

    def test_wrong_scheme(self, bot_config: MagicMock) -> None:
        headers = {"Authorization": "Basic test-bearer-token-abc123"}
        assert verify_bearer(headers, bot_config) is False

    def test_wrong_token(self, bot_config: MagicMock) -> None:
        headers = {"Authorization": "Bearer wrong-token-value"}
        assert verify_bearer(headers, bot_config) is False

    def test_lowercase_scheme(self, bot_config: MagicMock) -> None:
        # Scheme check is case-insensitive (.lower() == "bearer").
        headers = {"Authorization": "bearer test-bearer-token-abc123"}
        assert verify_bearer(headers, bot_config) is True

    def test_missing_config_field(self) -> None:
        # Build a fresh inline MagicMock; bot_api_token = None triggers
        # _get_bot_api_token to return None.
        bad_config = MagicMock()
        bad_config.http_api.bot_api_token = None
        headers = {"Authorization": "Bearer test-bearer-token-abc123"}
        assert verify_bearer(headers, bad_config) is False


# ---------------------------------------------------------------------------
# TestVerifyWsTicket — 10 tests
# ---------------------------------------------------------------------------

class TestVerifyWsTicket:
    """Tests for verify_ws_ticket(token, config)."""

    def test_valid_ticket(self, bot_config: MagicMock) -> None:
        secret = bot_config.http_api.ws_ticket_secret.get_secret_value()
        token = _make_ticket(secret)
        assert verify_ws_ticket(token, bot_config) is True

    def test_expired_ticket(self, bot_config: MagicMock) -> None:
        secret = bot_config.http_api.ws_ticket_secret.get_secret_value()
        token = _make_ticket(secret, exp=int(time.time()) - 10)
        assert verify_ws_ticket(token, bot_config) is False

    def test_tampered_signature(self, bot_config: MagicMock) -> None:
        secret = bot_config.http_api.ws_ticket_secret.get_secret_value()
        token = _make_ticket(secret)
        parts = token.split(".")
        # Replace the signature segment with garbage.
        parts[2] = "invalidsignatureXXXX"
        tampered = ".".join(parts)
        assert verify_ws_ticket(tampered, bot_config) is False

    def test_wrong_secret(self, bot_config: MagicMock) -> None:
        token = _make_ticket("different-secret-entirely")
        assert verify_ws_ticket(token, bot_config) is False

    def test_missing_sub(self, bot_config: MagicMock) -> None:
        secret = bot_config.http_api.ws_ticket_secret.get_secret_value()
        token = _make_ticket(secret, include_sub=False)
        assert verify_ws_ticket(token, bot_config) is False

    def test_missing_iat(self, bot_config: MagicMock) -> None:
        # Must include a valid sub so the sub guard (line 228) does NOT fire
        # first — the iat guard (line 234) is the intended failure point.
        secret = bot_config.http_api.ws_ticket_secret.get_secret_value()
        token = _make_ticket(secret, include_iat=False)
        assert verify_ws_ticket(token, bot_config) is False

    def test_malformed_token(self, bot_config: MagicMock) -> None:
        assert verify_ws_ticket("not-a-jwt", bot_config) is False

    def test_missing_config_field(self) -> None:
        # Build a fresh inline MagicMock; ws_ticket_secret = None triggers
        # _get_ws_ticket_secret to return None.
        bad_config = MagicMock()
        bad_config.http_api.ws_ticket_secret = None
        # Use a real secret just to build a syntactically valid token.
        token = _make_ticket("some-secret")
        assert verify_ws_ticket(token, bad_config) is False

    def test_single_use_replay(self, bot_config: MagicMock) -> None:
        secret = bot_config.http_api.ws_ticket_secret.get_secret_value()
        # Pin the sub so both calls use the same nonce.
        pinned_sub = f"replay-sub-{uuid.uuid4()}"
        token = _make_ticket(secret, sub=pinned_sub)
        first = verify_ws_ticket(token, bot_config)
        second = verify_ws_ticket(token, bot_config)
        assert first is True
        assert second is False

    def test_wrong_algorithm(self, bot_config: MagicMock) -> None:
        secret = bot_config.http_api.ws_ticket_secret.get_secret_value()
        token = _make_ticket(secret, algorithm="HS512")
        assert verify_ws_ticket(token, bot_config) is False
