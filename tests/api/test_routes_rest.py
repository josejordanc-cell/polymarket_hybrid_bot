"""tests/api/test_routes_rest.py — Integration tests for api/routes_rest.py.

Covers all three REST handlers registered by setup_routes:
    GET  /dashboard/snapshot
    POST /api/strategies/config
    POST /emergency-stop

Uses aiohttp.test_utils.TestClient + TestServer (part of aiohttp itself; no
pytest-aiohttp plugin required).  asyncio_mode = auto in pytest.ini means
async def test functions run without @pytest.mark.asyncio decorators.

Scope: function (default) — each test gets a fresh BotState via conftest.
"""
from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from api.routes_rest import setup_routes
from api.schemas import DashboardSnapshot, StopResponse, StrategyRuntimeConfig
from api.state import BotState


# ---------------------------------------------------------------------------
# Auth header helper
# ---------------------------------------------------------------------------


def _auth_headers(bot_config) -> dict[str, str]:
    token = bot_config.http_api.bot_api_token.get_secret_value()
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------


@pytest.fixture
async def test_client(bot_state, bot_config):
    """aiohttp TestClient bound to the real setup_routes registration."""
    app = web.Application()
    setup_routes(app, bot_state, bot_config)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    yield client
    await client.close()


# ---------------------------------------------------------------------------
# TestSnapshotHandler — GET /dashboard/snapshot
# ---------------------------------------------------------------------------


class TestSnapshotHandler:
    """9 tests covering auth, body shape, and OQ-3 stub fields."""

    async def test_snapshot_auth_success(self, test_client, bot_config):
        resp = await test_client.get(
            "/dashboard/snapshot", headers=_auth_headers(bot_config)
        )
        assert resp.status == 200
        body = await resp.json()
        DashboardSnapshot.model_validate(body)  # raises if schema violated

    async def test_snapshot_missing_auth(self, test_client):
        resp = await test_client.get("/dashboard/snapshot")
        assert resp.status == 401
        text = await resp.text()
        assert text == "Unauthorized"

    async def test_snapshot_wrong_token(self, test_client):
        resp = await test_client.get(
            "/dashboard/snapshot",
            headers={"Authorization": "Bearer wrong-token-xyz"},
        )
        assert resp.status == 401
        text = await resp.text()
        assert text == "Unauthorized"

    async def test_snapshot_wrong_scheme(self, test_client):
        resp = await test_client.get(
            "/dashboard/snapshot",
            headers={"Authorization": "Basic dGVzdA=="},
        )
        assert resp.status == 401

    async def test_snapshot_body_globalstats_present(self, test_client, bot_config):
        """v0.2.9 contract shape: 8-field globalStats; lastUpdatedAt lives
        inside globalStats (not at snapshot root) per audit row A.2."""
        resp = await test_client.get(
            "/dashboard/snapshot", headers=_auth_headers(bot_config)
        )
        assert resp.status == 200
        body = await resp.json()
        gs = body["globalStats"]
        assert gs["portfolioValue"] == 10000.0
        # equity=10000.0, day_start_equity=9500.0 -> pnlToday=500.0
        assert gs["pnlToday"] == 500.0
        # OQ-3 stubs — pipeline not wired
        assert gs["pnlMonth"] == 0.0
        assert gs["pnlAllTime"] == 0.0
        assert gs["winRate"] == 0.0
        assert gs["totalTrades"] == 0
        assert gs["streak"] == 0
        # lastUpdatedAt moved from snapshot root into globalStats per A.2
        assert isinstance(gs["lastUpdatedAt"], int)
        assert "lastUpdatedAt" not in body
        # Removed fields must not appear
        assert "totalEquity" not in gs
        assert "dayPnl" not in gs
        assert "dayPnlPct" not in gs
        # openPositions: int was bot-only per B.1; only the top-level
        # list[OpenPosition] (an OQ-3 stub) remains on the snapshot.
        assert "openPositions" not in gs

    async def test_snapshot_body_strategies_three_entries(self, test_client, bot_config):
        resp = await test_client.get(
            "/dashboard/snapshot", headers=_auth_headers(bot_config)
        )
        assert resp.status == 200
        body = await resp.json()
        strategies = body["strategies"]
        assert len(strategies) == 3
        ids = {s["id"] for s in strategies}
        assert ids == {"strategy1", "strategy2", "strategy3"}
        for s in strategies:
            # status changed "active"→"live" in D.2.4 step 3 to match
            # dashboard StrategyCardDataSchema enum ["live","paused"].
            # See d24-emit-site-audit.md §10 (STRATEGY_UPDATE drift).
            assert s["status"] == "live"

    async def test_snapshot_body_strategy_configs_present(self, test_client, bot_config):
        """v0.2.9 (L2 E.2): strategyId, riskPct (percent), maxPositionSize."""
        resp = await test_client.get(
            "/dashboard/snapshot", headers=_auth_headers(bot_config)
        )
        assert resp.status == 200
        body = await resp.json()
        configs = body["strategyConfigs"]
        assert len(configs) == 3
        ids = {cfg["strategyId"] for cfg in configs}
        assert ids == {"strategy1", "strategy2", "strategy3"}
        for cfg in configs:
            assert cfg["maxPositionSize"] == 100.0
            assert cfg["riskPct"] == 2.5
            # Removed/renamed fields must not appear
            assert "id" not in cfg
            assert "riskBudget" not in cfg

    async def test_snapshot_body_recent_fills_two_entries(self, test_client, bot_config):
        resp = await test_client.get(
            "/dashboard/snapshot", headers=_auth_headers(bot_config)
        )
        assert resp.status == 200
        body = await resp.json()
        fills = body["recentFills"]
        assert len(fills) == 2
        fill_ids = {f["id"] for f in fills}
        assert fill_ids == {"fill-001", "fill-002"}

    async def test_snapshot_body_oq3_stubs_empty(self, test_client, bot_config):
        resp = await test_client.get(
            "/dashboard/snapshot", headers=_auth_headers(bot_config)
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["openPositions"] == []
        assert body["equityHistory"] == []
        assert body["monthlyPnl"] == []
        assert body["backtest"] == []
        assert body["recentLogs"] == []
        assert body["recentAlerts"] == []


# ---------------------------------------------------------------------------
# TestStrategiesConfigHandler — POST /api/strategies/config
# ---------------------------------------------------------------------------


class TestStrategiesConfigHandler:
    """12 tests covering auth, JSON validation, Pydantic validation, and state mutation."""

    async def test_config_auth_success(self, test_client, bot_config):
        resp = await test_client.post(
            "/api/strategies/config",
            json={"strategyId": "strategy1", "enabled": False},
            headers=_auth_headers(bot_config),
        )
        assert resp.status == 200
        body = await resp.json()
        assert body == {"ok": True}

    async def test_config_missing_auth(self, test_client):
        resp = await test_client.post(
            "/api/strategies/config",
            json={"strategyId": "strategy1", "enabled": False},
        )
        assert resp.status == 401

    async def test_config_wrong_token(self, test_client):
        resp = await test_client.post(
            "/api/strategies/config",
            json={"strategyId": "strategy1", "enabled": False},
            headers={"Authorization": "Bearer wrong-token-xyz"},
        )
        assert resp.status == 401

    async def test_config_invalid_json(self, test_client, bot_config):
        resp = await test_client.post(
            "/api/strategies/config",
            data=b"not-json",
            headers={
                "Content-Type": "application/json",
                **_auth_headers(bot_config),
            },
        )
        assert resp.status == 400
        text = await resp.text()
        assert text == "Invalid JSON"

    async def test_config_empty_body(self, test_client, bot_config):
        resp = await test_client.post(
            "/api/strategies/config",
            data=b"",
            headers={
                "Content-Type": "application/json",
                **_auth_headers(bot_config),
            },
        )
        assert resp.status == 400
        text = await resp.text()
        assert text == "Invalid JSON"

    async def test_config_invalid_id(self, test_client, bot_config):
        resp = await test_client.post(
            "/api/strategies/config",
            json={"strategyId": "strategy99", "enabled": False},
            headers=_auth_headers(bot_config),
        )
        assert resp.status == 422

    async def test_config_invalid_max_position(self, test_client, bot_config):
        resp = await test_client.post(
            "/api/strategies/config",
            json={"strategyId": "strategy1", "maxPositionSize": 0.0},
            headers=_auth_headers(bot_config),
        )
        assert resp.status == 422

    async def test_config_invalid_risk_pct(self, test_client, bot_config):
        """v0.2.9 (L2 E.2): riskPct out-of-range (>10) must 422."""
        resp = await test_client.post(
            "/api/strategies/config",
            json={"strategyId": "strategy1", "riskPct": 10.5},
            headers=_auth_headers(bot_config),
        )
        assert resp.status == 422

    async def test_config_invalid_active_pairs(self, test_client, bot_config):
        """v0.2.9 (L2 E.2): activePairs is a list of BASE/QUOTE strings.
        Lowercase or non-slash-separated elements must 422."""
        resp = await test_client.post(
            "/api/strategies/config",
            json={"strategyId": "strategy1", "activePairs": ["btc-usd"]},
            headers=_auth_headers(bot_config),
        )
        assert resp.status == 422

    async def test_config_state_mutation(self, test_client, bot_config, bot_state):
        # POST the patch
        resp = await test_client.post(
            "/api/strategies/config",
            json={
                "strategyId": "strategy1",
                "enabled": False,
                "maxPositionSize": 75.0,
            },
            headers=_auth_headers(bot_config),
        )
        assert resp.status == 200

        # Verify via snapshot
        snap_resp = await test_client.get(
            "/dashboard/snapshot", headers=_auth_headers(bot_config)
        )
        assert snap_resp.status == 200
        snap_body = await snap_resp.json()
        configs = {c["strategyId"]: c for c in snap_body["strategyConfigs"]}
        assert configs["strategy1"]["enabled"] is False
        assert configs["strategy1"]["maxPositionSize"] == 75.0

    async def test_config_partial_patch_preserves_other_fields(
        self, test_client, bot_config
    ):
        # Only patch enabled; other fields must remain at defaults
        resp = await test_client.post(
            "/api/strategies/config",
            json={"strategyId": "strategy1", "enabled": False},
            headers=_auth_headers(bot_config),
        )
        assert resp.status == 200

        snap_resp = await test_client.get(
            "/dashboard/snapshot", headers=_auth_headers(bot_config)
        )
        assert snap_resp.status == 200
        snap_body = await snap_resp.json()
        configs = {c["strategyId"]: c for c in snap_body["strategyConfigs"]}
        s1 = configs["strategy1"]
        assert s1["enabled"] is False
        assert s1["riskPct"] == 2.5
        assert s1["maxPositionSize"] == 100.0

    async def test_config_sibling_strategies_untouched(
        self, test_client, bot_config
    ):
        # Patch strategy2 only; strategy1 and strategy3 must stay at defaults
        resp = await test_client.post(
            "/api/strategies/config",
            json={"strategyId": "strategy2", "enabled": False},
            headers=_auth_headers(bot_config),
        )
        assert resp.status == 200

        snap_resp = await test_client.get(
            "/dashboard/snapshot", headers=_auth_headers(bot_config)
        )
        assert snap_resp.status == 200
        snap_body = await snap_resp.json()
        configs = {c["strategyId"]: c for c in snap_body["strategyConfigs"]}
        assert configs["strategy1"]["enabled"] is True
        assert configs["strategy1"]["maxPositionSize"] == 100.0
        assert configs["strategy1"]["riskPct"] == 2.5
        assert configs["strategy3"]["enabled"] is True
        assert configs["strategy3"]["maxPositionSize"] == 100.0
        assert configs["strategy3"]["riskPct"] == 2.5


# ---------------------------------------------------------------------------
# TestEmergencyStopHandler — POST /emergency-stop
# ---------------------------------------------------------------------------


class TestEmergencyStopHandler:
    """7 tests covering auth, kill-switch mutation, response shape, idempotency, and ms timestamp."""

    async def test_stop_auth_success(self, test_client, bot_config):
        resp = await test_client.post(
            "/emergency-stop", headers=_auth_headers(bot_config)
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["ok"] is True

    async def test_stop_missing_auth(self, test_client):
        resp = await test_client.post("/emergency-stop")
        assert resp.status == 401

    async def test_stop_wrong_token(self, test_client):
        resp = await test_client.post(
            "/emergency-stop",
            headers={"Authorization": "Bearer wrong-token-xyz"},
        )
        assert resp.status == 401

    async def test_stop_sets_kill_switch(self, test_client, bot_config, bot_state):
        assert bot_state.kill_switch_requested is False
        resp = await test_client.post(
            "/emergency-stop", headers=_auth_headers(bot_config)
        )
        assert resp.status == 200
        assert bot_state.kill_switch_requested is True

    async def test_stop_response_body_shape(self, test_client, bot_config):
        resp = await test_client.post(
            "/emergency-stop", headers=_auth_headers(bot_config)
        )
        assert resp.status == 200
        body = await resp.json()
        # Validates against the contract schema
        stop_resp = StopResponse.model_validate(body)
        assert stop_resp.ok is True
        assert stop_resp.stoppedAt is not None
        assert stop_resp.stoppedAt > 1_700_000_000_000

    async def test_stop_idempotent(self, test_client, bot_config, bot_state):
        # POST twice — both must succeed and kill_switch stays True
        resp1 = await test_client.post(
            "/emergency-stop", headers=_auth_headers(bot_config)
        )
        assert resp1.status == 200

        resp2 = await test_client.post(
            "/emergency-stop", headers=_auth_headers(bot_config)
        )
        assert resp2.status == 200

        assert bot_state.kill_switch_requested is True

    async def test_stop_stopped_at_is_ms(self, test_client, bot_config):
        resp = await test_client.post(
            "/emergency-stop", headers=_auth_headers(bot_config)
        )
        assert resp.status == 200
        body = await resp.json()
        stopped_at = body["stoppedAt"]
        assert isinstance(stopped_at, int)
        # Valid epoch-ms range: 2023-11-14 to ~2033-05-18
        assert 1_700_000_000_000 <= stopped_at <= 2_000_000_000_000


# ---------------------------------------------------------------------------
# TestSnapshotDisabledStrategies — B.2 regression tests
# ---------------------------------------------------------------------------


@pytest.fixture
async def test_client_disabled_strategies(bot_config):
    """TestClient with strategy2 and strategy3 disabled in BotState."""
    state = BotState.create(equity=10000.0, day_start_equity=9500.0)
    state.strategy_configs["strategy2"] = StrategyRuntimeConfig(
        strategyId="strategy2", enabled=False
    )
    state.strategy_configs["strategy3"] = StrategyRuntimeConfig(
        strategyId="strategy3", enabled=False
    )
    app = web.Application()
    setup_routes(app, state, bot_config)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    yield client
    await client.close()


class TestSnapshotDisabledStrategies:
    """B.2 — snapshot shape when env strategy flags disable momentum/MM."""

    async def test_snapshot_strategies_length_three(
        self, test_client_disabled_strategies, bot_config
    ) -> None:
        """strategies array always has exactly 3 entries regardless of enabled state."""
        resp = await test_client_disabled_strategies.get(
            "/dashboard/snapshot", headers=_auth_headers(bot_config)
        )
        assert resp.status == 200
        body = await resp.json()
        assert len(body["strategies"]) == 3

    async def test_snapshot_strategy1_live_others_paused(
        self, test_client_disabled_strategies, bot_config
    ) -> None:
        """strategy1=live, strategy2/strategy3=paused when flags are False."""
        resp = await test_client_disabled_strategies.get(
            "/dashboard/snapshot", headers=_auth_headers(bot_config)
        )
        assert resp.status == 200
        body = await resp.json()
        by_id = {s["id"]: s for s in body["strategies"]}
        assert by_id["strategy1"]["status"] == "live"
        assert by_id["strategy2"]["status"] == "paused"
        assert by_id["strategy3"]["status"] == "paused"

    async def test_snapshot_strategies_deterministic_order(
        self, test_client_disabled_strategies, bot_config
    ) -> None:
        """strategies array order must be strategy1, strategy2, strategy3
        regardless of dict insertion order (B.2 reviewer Finding 6)."""
        resp = await test_client_disabled_strategies.get(
            "/dashboard/snapshot", headers=_auth_headers(bot_config)
        )
        assert resp.status == 200
        body = await resp.json()
        ids = [s["id"] for s in body["strategies"]]
        assert ids == ["strategy1", "strategy2", "strategy3"]

    async def test_snapshot_strategy_configs_deterministic_order(
        self, test_client_disabled_strategies, bot_config
    ) -> None:
        """strategyConfigs array order must also be strategy1, strategy2, strategy3."""
        resp = await test_client_disabled_strategies.get(
            "/dashboard/snapshot", headers=_auth_headers(bot_config)
        )
        assert resp.status == 200
        body = await resp.json()
        cfg_ids = [c["strategyId"] for c in body["strategyConfigs"]]
        assert cfg_ids == ["strategy1", "strategy2", "strategy3"]
