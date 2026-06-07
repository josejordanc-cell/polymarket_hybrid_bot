"""tests/api/test_trigger_fill.py — Integration tests for the sim-only
POST /debug/trigger-fill live-accounting verification hatch.

Uses aiohttp.test_utils.TestClient + TestServer (same pattern as
test_routes_rest.py; asyncio_mode = auto means async tests need no
decorator). Reuses the ``bot_config`` (MagicMock, runtime.mode="sim")
and ``bot_state`` (real BotState, 2 pre-seeded fills) fixtures from
tests/api/conftest.py.

Only ``risk`` is doubled (FakeRisk). The rest of the chain —
persist_fill, append_state_fill_record, broadcast_fill,
record_fill_return_for_correlation — runs the REAL production code, so
these tests exercise the wiring end-to-end. BOT_DATA_DIR is monkeypatched
to tmp_path so persist_fill writes stay hermetic.
"""
from __future__ import annotations

from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from api.routes_rest import setup_routes
from api.state import set_state as _set_state


# ---------------------------------------------------------------------------
# Test doubles + helpers
# ---------------------------------------------------------------------------


class FakeRisk:
    """Minimal RiskManager double exposing exactly the surface the
    trigger-fill chain touches: ``equity`` (read after on_fill),
    ``on_fill`` (mutates equity like the real BUY/SELL accounting), and
    ``record_return`` (called by record_fill_return_for_correlation)."""

    def __init__(self, equity: float = 10_000.0) -> None:
        self.equity = equity
        self.on_fill_calls: list[dict[str, Any]] = []
        self.returns: list[tuple[str, float]] = []

    def on_fill(self, intent: Any, fill_price: float, filled_size: float) -> None:
        self.on_fill_calls.append(
            {"side": intent.side, "fill_price": fill_price, "filled_size": filled_size}
        )
        cost = fill_price * filled_size
        if intent.side == "BUY":
            self.equity -= cost
        else:
            self.equity += cost

    def record_return(self, market_id: str, value: float) -> None:
        self.returns.append((market_id, value))


def _auth_headers(bot_config) -> dict[str, str]:
    token = bot_config.http_api.bot_api_token.get_secret_value()
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def make_trigger_client(bot_state, bot_config):
    """Factory: build a started TestClient whose /debug/trigger-fill route
    is wired to a caller-supplied risk double. Returns ``client``.
    All created clients + the state singleton are torn down at the end."""
    created: list[TestClient] = []

    async def _make(risk: Any) -> TestClient:
        app = web.Application()
        setup_routes(app, bot_state, bot_config, risk)
        _set_state(bot_state)
        server = TestServer(app)
        client = TestClient(server)
        await client.start_server()
        created.append(client)
        return client

    try:
        yield _make
    finally:
        for c in created:
            await c.close()
        _set_state(None)


_VALID_BODY = {
    "market_id": "MKT-TRIGGER-001",
    "outcome": "YES",
    "side": "BUY",
    "size": 10.0,
    "price": 0.40,
    "strategy": "intra_arbitrage",  # internal name → wire strategy1
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestTriggerFill:
    async def test_404_when_not_sim(self, make_trigger_client, bot_config):
        # Gate 1: live mode 404s BEFORE auth, even with a valid bearer.
        bot_config.runtime.mode = "live"
        risk = FakeRisk()
        client = await make_trigger_client(risk)
        resp = await client.post(
            "/debug/trigger-fill",
            json=_VALID_BODY,
            headers=_auth_headers(bot_config),
        )
        assert resp.status == 404
        # No accounting should have happened.
        assert risk.on_fill_calls == []
        assert risk.equity == 10_000.0

    async def test_401_bad_bearer_in_sim(self, make_trigger_client, bot_config):
        risk = FakeRisk()
        client = await make_trigger_client(risk)
        # Wrong token.
        resp = await client.post(
            "/debug/trigger-fill",
            json=_VALID_BODY,
            headers={"Authorization": "Bearer wrong-token-xyz"},
        )
        assert resp.status == 401
        # No header at all.
        resp2 = await client.post("/debug/trigger-fill", json=_VALID_BODY)
        assert resp2.status == 401
        assert risk.on_fill_calls == []

    async def test_400_malformed_body(self, make_trigger_client, bot_config):
        risk = FakeRisk()
        client = await make_trigger_client(risk)
        resp = await client.post(
            "/debug/trigger-fill",
            data="this is not json",
            headers=_auth_headers(bot_config),
        )
        assert resp.status == 400
        # And a well-formed-JSON-but-schema-invalid body also 400s.
        resp2 = await client.post(
            "/debug/trigger-fill",
            json={**_VALID_BODY, "price": 2.0},  # price must be <= 1.0
            headers=_auth_headers(bot_config),
        )
        assert resp2.status == 400
        assert risk.on_fill_calls == []

    async def test_500_when_risk_unavailable(self, make_trigger_client, bot_config):
        # Defensive path: sim + auth OK but risk wasn't stashed.
        client = await make_trigger_client(None)
        resp = await client.post(
            "/debug/trigger-fill",
            json=_VALID_BODY,
            headers=_auth_headers(bot_config),
        )
        assert resp.status == 500
        assert "live risk unavailable" in await resp.text()

    async def test_happy_path_drives_full_chain(
        self, make_trigger_client, bot_config, bot_state, tmp_path, monkeypatch
    ):
        # Keep persist_fill hermetic.
        monkeypatch.setenv("BOT_DATA_DIR", str(tmp_path))

        risk = FakeRisk(equity=10_000.0)
        fills_before = len(bot_state.recent_fills)
        client = await make_trigger_client(risk)

        resp = await client.post(
            "/debug/trigger-fill",
            json=_VALID_BODY,
            headers=_auth_headers(bot_config),
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["status"] == "filled"
        assert body["market_id"] == "MKT-TRIGGER-001"

        # 1) risk.on_fill ran and moved equity (BUY → debit cost = 0.40*10).
        assert len(risk.on_fill_calls) == 1
        assert risk.equity == pytest.approx(10_000.0 - 0.40 * 10.0)
        assert body["equity_after"] == pytest.approx(risk.equity)

        # 2) record_fill_return_for_correlation fed risk.record_return.
        assert len(risk.returns) == 1
        assert risk.returns[0][0] == "MKT-TRIGGER-001"

        # 3) persist_fill wrote one trades.jsonl line.
        trades_file = tmp_path / "trades.jsonl"
        assert trades_file.exists()
        lines = [ln for ln in trades_file.read_text().splitlines() if ln.strip()]
        assert len(lines) == 1
        assert "MKT-TRIGGER-001" in lines[0]

        # 4) append_state_fill_record populated BotState (appendleft → newest
        #    at index 0).
        assert len(bot_state.recent_fills) == fills_before + 1
        newest = bot_state.recent_fills[0]
        assert newest.symbol == "MKT-TRIGGER-001"[:12]
        assert newest.status == "filled"
