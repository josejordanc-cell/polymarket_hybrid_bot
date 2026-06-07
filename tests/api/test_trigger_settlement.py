"""tests/api/test_trigger_settlement.py — Integration tests for the
sim-only POST /debug/trigger-settlement verification hatch.

Mirrors test_trigger_fill.py (aiohttp TestClient + TestServer; asyncio_mode
= auto). Reuses the ``bot_config`` (MagicMock, runtime.mode="sim") and
``bot_state`` (real BotState) fixtures from tests/api/conftest.py.

Only ``risk`` is doubled. FakeRisk models equity and realised_pnl as
DISTINCT quantities — exactly as RiskManager.on_fill / on_settlement do —
which is the whole point of the corrected loop the hatch replicates: a loss
moves realised_pnl negative while equity (proceeds=0) stays flat. The real
``persist_settlement`` runs; BOT_DATA_DIR is monkeypatched to tmp_path so
the settlements.jsonl write stays hermetic.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from api.routes_rest import setup_routes
from api.state import set_state as _set_state
from utils.derived_stats import compute_win_rate


# ---------------------------------------------------------------------------
# Test doubles + helpers
# ---------------------------------------------------------------------------


class _FakePosition:
    def __init__(
        self, market_id: str, outcome: str, quantity: float, avg_price: float
    ) -> None:
        self.market_id = market_id
        self.outcome = outcome
        self.quantity = quantity
        self.avg_price = avg_price

    @property
    def notional_usd(self) -> float:
        return self.quantity * self.avg_price


class FakeRisk:
    """Faithful RiskManager double modelling equity and realised_pnl as
    DISTINCT quantities (same model the resolution-loop stub uses):
      * on_fill(BUY) debits cost and creates open_positions[f"{mid}_{outcome}"]
      * on_settlement adds proceeds to equity and (proceeds - notional) to
        realised_pnl, then pops the position
      * daily_pnl_snapshot() exposes realised_pnl_usd
    """

    def __init__(self, equity: float = 10_000.0) -> None:
        self.equity = equity
        self.realised_pnl = 0.0
        self.open_positions: dict[str, _FakePosition] = {}
        self.on_fill_calls: list[tuple[str, float, float]] = []
        self.on_settlement_calls: list[tuple[str, str]] = []

    def on_fill(self, intent: Any, fill_price: float, filled_size: float) -> None:
        self.on_fill_calls.append((intent.side, fill_price, filled_size))
        key = f"{intent.market_id}_{intent.outcome}"
        cost = fill_price * filled_size
        if intent.side == "BUY":
            self.equity -= cost
            self.open_positions[key] = _FakePosition(
                intent.market_id, intent.outcome, filled_size, fill_price
            )
        # SELL branch is never exercised by the settlement hatch.

    def daily_pnl_snapshot(self) -> SimpleNamespace:
        return SimpleNamespace(realised_pnl_usd=self.realised_pnl)

    def on_settlement(self, market_id: str, winning_outcome: str) -> None:
        self.on_settlement_calls.append((market_id, winning_outcome))
        for key in list(self.open_positions.keys()):
            pos = self.open_positions[key]
            if pos.market_id != market_id:
                continue
            proceeds = pos.quantity * 1.0 if pos.outcome == winning_outcome else 0.0
            pnl = proceeds - pos.notional_usd
            self.equity += proceeds
            self.realised_pnl += pnl
            del self.open_positions[key]


def _auth_headers(bot_config) -> dict[str, str]:
    token = bot_config.http_api.bot_api_token.get_secret_value()
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def make_settlement_client(bot_state, bot_config):
    """Factory: build a started TestClient whose /debug/trigger-settlement
    route is wired to a caller-supplied risk double. Returns ``client``.
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
    "market_id": "MKT-SETTLE-001",
    "outcome": "YES",
    "size": 10.0,
    "price": 0.40,
    "strategy": "intra_arbitrage",
    "win": True,
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestTriggerSettlement:
    async def test_404_when_not_sim(self, make_settlement_client, bot_config):
        # Gate 1: live mode 404s BEFORE auth, even with a valid bearer.
        bot_config.runtime.mode = "live"
        risk = FakeRisk()
        client = await make_settlement_client(risk)
        resp = await client.post(
            "/debug/trigger-settlement",
            json=_VALID_BODY,
            headers=_auth_headers(bot_config),
        )
        assert resp.status == 404
        # No accounting should have happened.
        assert risk.on_fill_calls == []
        assert risk.on_settlement_calls == []

    async def test_401_bad_bearer_in_sim(self, make_settlement_client, bot_config):
        risk = FakeRisk()
        client = await make_settlement_client(risk)
        # Wrong token.
        resp = await client.post(
            "/debug/trigger-settlement",
            json=_VALID_BODY,
            headers={"Authorization": "Bearer wrong-token-xyz"},
        )
        assert resp.status == 401
        # No header at all.
        resp2 = await client.post("/debug/trigger-settlement", json=_VALID_BODY)
        assert resp2.status == 401
        assert risk.on_fill_calls == []
        assert risk.on_settlement_calls == []

    async def test_400_malformed_body(self, make_settlement_client, bot_config):
        risk = FakeRisk()
        client = await make_settlement_client(risk)
        resp = await client.post(
            "/debug/trigger-settlement",
            data="this is not json",
            headers=_auth_headers(bot_config),
        )
        assert resp.status == 400
        # And a well-formed-JSON-but-schema-invalid body also 400s (missing win).
        resp2 = await client.post(
            "/debug/trigger-settlement",
            json={k: v for k, v in _VALID_BODY.items() if k != "win"},
            headers=_auth_headers(bot_config),
        )
        assert resp2.status == 400
        assert risk.on_fill_calls == []
        assert risk.on_settlement_calls == []

    async def test_400_when_price_is_one(self, make_settlement_client, bot_config):
        # The break-even guard: price==1.0 makes a win size*(1-1)=0, which
        # winRate/streak exclude — rejected at validation.
        risk = FakeRisk()
        client = await make_settlement_client(risk)
        resp = await client.post(
            "/debug/trigger-settlement",
            json={**_VALID_BODY, "price": 1.0},
            headers=_auth_headers(bot_config),
        )
        assert resp.status == 400
        assert risk.on_fill_calls == []
        assert risk.on_settlement_calls == []

    async def test_500_when_risk_unavailable(self, make_settlement_client, bot_config):
        # Defensive path: sim + auth OK but risk wasn't stashed.
        client = await make_settlement_client(None)
        resp = await client.post(
            "/debug/trigger-settlement",
            json=_VALID_BODY,
            headers=_auth_headers(bot_config),
        )
        assert resp.status == 500
        assert "live risk unavailable" in await resp.text()

    async def test_happy_win_persists_positive_pnl(
        self, make_settlement_client, bot_config, tmp_path, monkeypatch
    ):
        import json

        monkeypatch.setenv("BOT_DATA_DIR", str(tmp_path))

        risk = FakeRisk(equity=10_000.0)
        client = await make_settlement_client(risk)

        resp = await client.post(
            "/debug/trigger-settlement",
            json=_VALID_BODY,  # win=True, YES @ 0.40 x 10
            headers=_auth_headers(bot_config),
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["status"] == "settled"
        assert body["market_id"] == "MKT-SETTLE-001"
        assert body["win"] is True

        # Seed BUY YES qty10 @0.40 → win → proceeds=10, notional=4,
        # realised_pnl delta = 10 - 4 = +6 (> 0).
        assert risk.on_fill_calls == [("BUY", 0.40, 10.0)]
        assert risk.on_settlement_calls == [("MKT-SETTLE-001", "YES")]
        assert body["pnl"] == pytest.approx(6.0)
        assert body["pnl"] > 0
        assert body["equity_after"] == pytest.approx(risk.equity)

        # settlements.jsonl row with pnl>0 / winning=True.
        out = tmp_path / "settlements.jsonl"
        assert out.is_file()
        rows = [json.loads(ln) for ln in out.read_text().splitlines() if ln.strip()]
        assert len(rows) == 1
        row = rows[0]
        assert row["resolved_outcome"] == "YES"
        assert row["pnl"] == pytest.approx(6.0)
        assert row["winning"] is True

        # Read side reflects the win (0–100 scale).
        assert compute_win_rate() == 100.0

    async def test_happy_loss_persists_negative_pnl(
        self, make_settlement_client, bot_config, tmp_path, monkeypatch
    ):
        import json

        monkeypatch.setenv("BOT_DATA_DIR", str(tmp_path))

        risk = FakeRisk(equity=10_000.0)
        client = await make_settlement_client(risk)

        resp = await client.post(
            "/debug/trigger-settlement",
            json={**_VALID_BODY, "win": False},  # seeded YES loses
            headers=_auth_headers(bot_config),
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["status"] == "settled"
        assert body["win"] is False

        # Seed BUY YES qty10 @0.40 → win=False → winning_outcome=NO → the YES
        # leg loses: proceeds=0, realised_pnl delta = 0 - 4 = -4 (< 0).
        # Equity unchanged by settlement (proceeds=0).
        assert risk.on_settlement_calls == [("MKT-SETTLE-001", "NO")]
        assert body["pnl"] == pytest.approx(-4.0)
        assert body["pnl"] < 0

        out = tmp_path / "settlements.jsonl"
        rows = [json.loads(ln) for ln in out.read_text().splitlines() if ln.strip()]
        assert len(rows) == 1
        row = rows[0]
        assert row["resolved_outcome"] == "NO"
        assert row["pnl"] == pytest.approx(-4.0)
        assert row["winning"] is False

        # Read side reflects the loss (no wins → 0.0).
        assert compute_win_rate() == 0.0
