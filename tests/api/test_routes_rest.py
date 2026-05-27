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

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from api.routes_rest import (
    _collapse_position_id,
    _map_alert_severity,
    setup_routes,
)
from api.schemas import (
    DashboardSnapshot,
    OpenPosition,
    StopResponse,
    StrategyRuntimeConfig,
)
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
    """aiohttp TestClient bound to the real setup_routes registration.

    v0.2.9 b9-fix: also registers the BotState singleton via
    ``set_state(bot_state)`` so handler code paths that go through
    ``utils.alerts.send_alert`` (e.g. POST /debug/inject-alert) see the
    same BotState the snapshot handler reads. Mirrors the production
    wiring in ``api/server.py``. Teardown clears the singleton."""
    from api.state import set_state as _set_state_for_test

    app = web.Application()
    setup_routes(app, bot_state, bot_config)
    _set_state_for_test(bot_state)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        yield client
    finally:
        await client.close()
        _set_state_for_test(None)


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

    async def test_snapshot_body_globalstats_present(
        self, test_client, bot_config, tmp_path, monkeypatch
    ):
        """v0.2.9 contract shape: 8-field globalStats; lastUpdatedAt lives
        inside globalStats (not at snapshot root) per audit row A.2.

        Isolates BOT_DATA_DIR to tmp_path so the three file-derived
        GlobalStats fields (winRate, totalTrades, streak — wired in PR1
        and PR2 of the profitability epic) read an empty directory and
        return their deterministic zero defaults, regardless of any
        residual data/{trades,settlements}.jsonl in the working tree."""
        monkeypatch.setenv("BOT_DATA_DIR", str(tmp_path))
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
        # lastUpdatedAt moved from snapshot root into globalStats per A.2.
        # v0.2.9 3d-pre: must be a live epoch-ms timestamp (positive int,
        # within ~1s of "now"), not the 0 sentinel that defaulted from
        # BotState.last_updated_at_ms before the strategy loop wires a fill.
        # Dashboard's refreshDashboard.ts stale-snapshot gate compares
        # incomingTs ≤ storedTs (BOT_CONTRACT.md v1.0 line 186); 0 always
        # loses against the portfolioStore seed (Date.now()).
        import time as _time
        now_ms = int(_time.time() * 1000)
        assert isinstance(gs["lastUpdatedAt"], int)
        assert gs["lastUpdatedAt"] > 0
        assert now_ms - 1000 <= gs["lastUpdatedAt"] <= now_ms + 1000
        assert "lastUpdatedAt" not in body
        # Removed fields must not appear
        assert "totalEquity" not in gs
        assert "dayPnl" not in gs
        assert "dayPnlPct" not in gs
        # openPositions: int was bot-only per B.1; the top-level
        # list[OpenPosition] (an OQ-3 stub) was renamed positions in
        # v0.2.9 audit row A.1.
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
            # v0.2.9 3d-pre: activePairs ships [] not null when no pairs
            # are configured (BOT_CONTRACT.md v1.0 line 121).
            assert cfg["activePairs"] == []
            assert isinstance(cfg["activePairs"], list)
            assert cfg["riskPct"] == 2.5
            # Removed/renamed fields must not appear
            assert "id" not in cfg
            assert "riskBudget" not in cfg

    async def test_snapshot_strategy_configs_active_pairs_never_null(
        self, test_client, bot_config
    ):
        """v0.2.9 3d-pre: dedicated guard against the activePairs:null
        regression. Every strategyConfig in the wire payload must have
        activePairs as a list (possibly empty) — never null. Dashboard
        Zod (z.array(...) required, not nullable, BOT_CONTRACT.md v1.0
        line 121) silently rejects rows where activePairs is null,
        which caused the strategy panel to fall back to mocks during
        joint smoke #1."""
        resp = await test_client.get(
            "/dashboard/snapshot", headers=_auth_headers(bot_config)
        )
        assert resp.status == 200
        body = await resp.json()
        assert all(
            isinstance(c["activePairs"], list)
            for c in body["strategyConfigs"]
        )
        assert all(c["activePairs"] is not None for c in body["strategyConfigs"])

    async def test_snapshot_body_recent_fills_two_entries(self, test_client, bot_config):
        resp = await test_client.get(
            "/dashboard/snapshot", headers=_auth_headers(bot_config)
        )
        assert resp.status == 200
        body = await resp.json()
        fills = body["fills"]
        assert len(fills) == 2
        fill_ids = {f["id"] for f in fills}
        assert fill_ids == {"fill-001", "fill-002"}

    async def test_snapshot_body_oq3_stubs_empty(self, test_client, bot_config):
        """v0.2.9 audit row A.1: six top-level renames are reflected in the
        wire payload. The `risk` field emits a zero-arg RiskMetrics() stub
        (dict, not null) to satisfy dashboard's Zod object-shape requirement."""
        resp = await test_client.get(
            "/dashboard/snapshot", headers=_auth_headers(bot_config)
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["positions"] == []
        assert body["equity"] == []
        assert body["monthlyPnl"] == []
        assert body["backtest"] == []
        assert body["logs"] == []
        assert body["alerts"] == []
        # risk: zero-arg RiskMetrics() stub — canonical 6-field shape
        # per v0.2.9 3e (components/dashboard/schemas.ts:96 +
        # BOT_CONTRACT.md v1.0 line 119). leverage=1.0 (spot-venue
        # structural constant). v0.2.9 B.1 (PATH C) populates the
        # other 5 fields via utils.risk_metrics helpers:
        #   - maxDrawdown / sharpe / var95: null (fixture has no
        #     equity_history; helpers return None for insufficient data)
        #   - exposurePct: 0.0 (empty positions over positive equity =
        #     0% exposure per lock 7 — semantically NOT null)
        #   - liquidationRisk: "low" (fixture equity=10000 > day_start=9500
        #     → no intra-day drawdown → lock-11 threshold table returns "low")
        assert body["risk"] is not None
        assert isinstance(body["risk"], dict)
        assert list(body["risk"].keys()) == [
            "maxDrawdown",
            "sharpe",
            "var95",
            "leverage",
            "exposurePct",
            "liquidationRisk",
        ]
        assert body["risk"]["leverage"] == 1.0
        assert body["risk"]["maxDrawdown"] is None
        assert body["risk"]["sharpe"] is None
        assert body["risk"]["var95"] is None
        assert body["risk"]["exposurePct"] == 0.0
        assert body["risk"]["liquidationRisk"] == "low"
        # Legacy 5bc2b6b stub field names must NOT appear in the wire payload.
        for legacy_risk_key in (
            "currentDrawdown",
            "sharpeRatio",
            "kellyFraction",
            "pairCorrelation",
            "dailyVaR",
        ):
            assert legacy_risk_key not in body["risk"]
        # Old key names must NOT appear in the wire payload.
        for legacy_key in (
            "openPositions",
            "recentFills",
            "riskMetrics",
            "equityHistory",
            "recentLogs",
            "recentAlerts",
        ):
            assert legacy_key not in body, (
                f"v0.2.9 audit row A.1: legacy key {legacy_key!r} must not "
                f"appear in snapshot wire payload"
            )


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


# ---------------------------------------------------------------------------
# Test_collapse_position_id — boundary helper (v0.2.9 audit B.3 / L2 E.1)
# ---------------------------------------------------------------------------


class TestCollapsePositionId:
    """Unit tests for the (marketId, outcome) → single-id collapse rule.

    Accepts both wire-style camelCase (marketId) and internal snake_case
    (market_id) inputs; covers Polymarket and spot-venue patterns; raises
    on unknown shapes so wire-id corruption surfaces at the boundary.
    """

    def test_polymarket_dict_camel_case(self) -> None:
        assert _collapse_position_id(
            {"marketId": "abc", "outcome": "yes"}
        ) == "abc:yes"

    def test_polymarket_dict_snake_case(self) -> None:
        """utils.types.Position uses snake_case market_id; the helper
        must accept that form so internal Position objects flow through
        without renaming at the source."""
        assert _collapse_position_id(
            {"market_id": "MKT-001", "outcome": "YES"}
        ) == "MKT-001:YES"

    def test_spot_dict(self) -> None:
        assert _collapse_position_id(
            {"venue": "binance", "symbol": "BTC/USDT"}
        ) == "binance:BTC/USDT"

    def test_polymarket_object(self) -> None:
        """Accepts object inputs via getattr (e.g. utils.types.Position)."""

        class P:
            market_id = "obj-mkt"
            outcome = "NO"

        assert _collapse_position_id(P()) == "obj-mkt:NO"

    def test_polymarket_takes_precedence_over_spot(self) -> None:
        """If both patterns' fields are present, Polymarket wins —
        because that's the bot's primary market type and the internal
        Position model only has marketId/outcome."""
        assert _collapse_position_id(
            {
                "marketId": "poly-mkt",
                "outcome": "YES",
                "venue": "binance",
                "symbol": "BTC/USDT",
            }
        ) == "poly-mkt:YES"

    def test_unknown_shape_raises(self) -> None:
        """Unknown position shapes must raise rather than silently
        corrupt the wire id."""
        with pytest.raises(ValueError):
            _collapse_position_id({"foo": "bar"})
        with pytest.raises(ValueError):
            _collapse_position_id({})


# ---------------------------------------------------------------------------
# test_open_position_wire_shape_matches_zod (v0.2.9 3d-fix)
# ---------------------------------------------------------------------------


def test_open_position_wire_shape_matches_zod() -> None:
    """v0.2.9 3d-fix regression guard. Constructs an OpenPosition from a
    representative internal Position fixture using the locked policy
    (Jose, 2026-05-21) — tradeId=id, currentPrice=entryPrice,
    unrealizedPnl{Pct,Usd}=0.0, timeOpen=ISO-8601-UTC-with-Z — and
    asserts the model_dump output's key SEQUENCE matches the 11-field
    Zod set in exact order per
    ``crypto-trading-bot-dashboard/components/dashboard/schemas.ts:67``
    and ``BOT_CONTRACT.md`` v1.0 line 115.

    The hardcoded key list is the regression contract: if Zod adds,
    removes, or reorders a field, this test must fail.
    """
    from datetime import datetime, timezone

    # Internal Position-shaped fixture (mirrors utils.types.Position).
    internal_pos = {
        "market_id": "0xabc",
        "outcome": "YES",
        "quantity": 50.0,
        "avg_price": 0.55,
        "strategy": "strategy1",
        "opened_at": datetime(2026, 5, 21, 13, 0, 0, tzinfo=timezone.utc),
    }

    # Apply the locked-policy conversion (the same logic that lands in
    # _build_snapshot when B.1 fill runtime ships).
    pos_id = _collapse_position_id(internal_pos)
    time_open = (
        internal_pos["opened_at"]
        .astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )
    direction = "Long" if internal_pos["outcome"] == "YES" else "Short"
    entry_price = internal_pos["avg_price"]

    wire = OpenPosition(
        id=pos_id,
        tradeId=pos_id,                           # stopgap: same as id
        symbol=internal_pos["market_id"][:12],    # display-only truncate
        strategy=internal_pos["strategy"],
        direction=direction,
        entryPrice=entry_price,
        currentPrice=entry_price,                  # stopgap: mark = entry
        size=internal_pos["quantity"],
        unrealizedPnlPct=0.0,                      # stopgap: needs mark
        unrealizedPnlUsd=0.0,                      # stopgap: needs mark
        timeOpen=time_open,
    )

    data = wire.model_dump(mode="json")

    # 1. Exact 11-field key set, in Zod order.
    expected_keys = [
        "id",
        "tradeId",
        "symbol",
        "strategy",
        "direction",
        "entryPrice",
        "currentPrice",
        "size",
        "unrealizedPnlPct",
        "unrealizedPnlUsd",
        "timeOpen",
    ]
    assert list(data.keys()) == expected_keys

    # 2. tradeId equals id under the v0.2.9 3d-fix stopgap policy.
    assert data["tradeId"] == data["id"] == "0xabc:YES"

    # 3. direction is title case (BOT_CONTRACT.md line 472).
    assert data["direction"] in ("Long", "Short")
    assert data["direction"] == "Long"

    # 4. timeOpen is ISO 8601 UTC with Z suffix — regex check.
    import re
    assert re.match(
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$", data["timeOpen"]
    ), f"timeOpen {data['timeOpen']!r} does not match ISO-8601-UTC-Z"
    assert data["timeOpen"] == "2026-05-21T13:00:00Z"

    # 5. PnL stopgaps: both 0.0 and float-typed.
    assert data["unrealizedPnlPct"] == 0.0
    assert data["unrealizedPnlUsd"] == 0.0
    assert isinstance(data["unrealizedPnlPct"], float)
    assert isinstance(data["unrealizedPnlUsd"], float)

    # 6. currentPrice == entryPrice (stopgap: mark trivially equals entry).
    assert data["currentPrice"] == data["entryPrice"]


# ---------------------------------------------------------------------------
# (TestMapLogLevel moved to tests/utils/test_logs.py alongside the
# helper's new module — v0.2.9 B.8.)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# TestMapAlertSeverity — E.3 collapse (v0.2.9 3f, audit row B.9)
# ---------------------------------------------------------------------------


class TestMapAlertSeverity:
    """Internal severity → wire AlertItem.severity map.

    Locked policy (E.3 collapse, Jose 2026-05-21):
      debug    → None (drop entry)
      info     → "info"
      warning  → "warning"
      error    → "critical"   (E.3 collapse to highest wire tier)
      critical → "critical"
    """

    def test_debug_drops(self) -> None:
        assert _map_alert_severity("debug") is None

    def test_info_maps_to_info(self) -> None:
        assert _map_alert_severity("info") == "info"

    def test_warning_maps_to_warning(self) -> None:
        assert _map_alert_severity("warning") == "warning"

    def test_error_collapses_to_critical(self) -> None:
        """E.3: internal error → wire critical (no separate error tier)."""
        assert _map_alert_severity("error") == "critical"

    def test_critical_maps_to_critical(self) -> None:
        assert _map_alert_severity("critical") == "critical"

    def test_unknown_raises(self) -> None:
        """Unknown internal severities raise rather than silently corrupt
        the wire enum."""
        with pytest.raises(ValueError):
            _map_alert_severity("notice")
        with pytest.raises(ValueError):
            _map_alert_severity("INFO")  # uppercase wrong; this is wire-side
        with pytest.raises(ValueError):
            _map_alert_severity("")


# ---------------------------------------------------------------------------
# TestBuildOpenPosition + snapshot positions integration (v0.2.9 3h step 1)
# ---------------------------------------------------------------------------


from datetime import datetime, timezone

from api.routes_rest import _build_open_position
from utils.types import Position as InternalPosition


class TestBuildOpenPosition:
    """Production helper that turns an internal Position into the
    11-field wire OpenPosition. Mirrors the locked-policy mapping
    that 3d-fix established and 3h promotes from comment to live code."""

    def _make_internal(self, **overrides) -> InternalPosition:
        defaults = dict(
            market_id="0xabc",
            outcome="YES",
            quantity=50.0,
            avg_price=0.55,
            strategy="strategy1",
            opened_at=datetime(2026, 5, 21, 13, 0, 0, tzinfo=timezone.utc),
        )
        defaults.update(overrides)
        return InternalPosition(**defaults)

    def test_full_11_field_shape(self) -> None:
        wire = _build_open_position(self._make_internal())
        data = wire.model_dump(mode="json")
        assert list(data.keys()) == [
            "id",
            "tradeId",
            "symbol",
            "strategy",
            "direction",
            "entryPrice",
            "currentPrice",
            "size",
            "unrealizedPnlPct",
            "unrealizedPnlUsd",
            "timeOpen",
        ]

    def test_polymarket_position_yes_direction_long(self) -> None:
        wire = _build_open_position(self._make_internal(outcome="YES"))
        assert wire.id == "0xabc:YES"
        assert wire.tradeId == "0xabc:YES"  # stopgap: same as id
        assert wire.direction == "Long"
        assert wire.symbol == "0xabc"
        assert wire.strategy == "strategy1"
        assert wire.entryPrice == 0.55
        assert wire.currentPrice == 0.55       # stopgap: mark = entry
        assert wire.size == 50.0
        assert wire.unrealizedPnlPct == 0.0    # stopgap
        assert wire.unrealizedPnlUsd == 0.0    # stopgap
        assert wire.timeOpen == "2026-05-21T13:00:00Z"

    def test_polymarket_position_no_direction_short(self) -> None:
        wire = _build_open_position(self._make_internal(outcome="NO"))
        assert wire.id == "0xabc:NO"
        assert wire.direction == "Short"

    def test_market_id_truncated_to_12_chars_for_symbol(self) -> None:
        long_id = "0x" + "a" * 60
        wire = _build_open_position(self._make_internal(market_id=long_id))
        assert wire.symbol == long_id[:12]
        # id (collapsed) preserves the full market_id, not the truncate.
        assert wire.id == f"{long_id}:YES"


class TestSnapshotPositionsAfterInjection:
    """Integration: snapshot's positions[] populates when BotState has
    open_positions written via the helper-direct path (no debug endpoint)."""

    async def test_snapshot_positions_serialise(
        self, test_client, bot_config, bot_state
    ) -> None:
        # Inject directly into the fixture's BotState (bypass the debug
        # endpoint so the integration test stays focused on the snapshot
        # serialization path).
        pos = InternalPosition(
            market_id="0xdead",
            outcome="YES",
            quantity=10.0,
            avg_price=0.42,
            strategy="strategy2",
            opened_at=datetime(2026, 5, 21, 13, 5, 0, tzinfo=timezone.utc),
        )
        bot_state.open_positions["0xdead:YES"] = pos

        resp = await test_client.get(
            "/dashboard/snapshot", headers=_auth_headers(bot_config)
        )
        assert resp.status == 200
        body = await resp.json()
        assert len(body["positions"]) == 1
        wire = body["positions"][0]
        assert wire["id"] == "0xdead:YES"
        assert wire["direction"] == "Long"
        assert wire["entryPrice"] == 0.42
        assert wire["size"] == 10.0
        assert wire["currentPrice"] == 0.42   # stopgap
        assert wire["unrealizedPnlPct"] == 0.0
        assert wire["unrealizedPnlUsd"] == 0.0
        assert wire["timeOpen"] == "2026-05-21T13:05:00Z"


# ---------------------------------------------------------------------------
# TestInjectPositionHandler — sim-mode-only debug endpoint (v0.2.9 3h step 2)
# ---------------------------------------------------------------------------


def _inject_payload(**overrides) -> dict:
    base = {
        "market_id": "0xabc",
        "outcome": "YES",
        "quantity": 50.0,
        "avg_price": 0.55,
        "strategy": "strategy1",
        "opened_at": "2026-05-21T13:00:00Z",
    }
    base.update(overrides)
    return base


class TestInjectPositionHandler:
    """POST /debug/inject-position. Sim-mode-only; Bearer-auth-required;
    writes BotState.open_positions AND BotState.recent_fills atomically."""

    async def test_sim_mode_200(self, test_client, bot_config, bot_state):
        resp = await test_client.post(
            "/debug/inject-position",
            json=_inject_payload(),
            headers=_auth_headers(bot_config),
        )
        assert resp.status == 200
        body = await resp.json()
        assert body == {"id": "0xabc:YES", "status": "injected"}
        # BotState.open_positions populated.
        assert "0xabc:YES" in bot_state.open_positions
        pos = bot_state.open_positions["0xabc:YES"]
        assert pos.market_id == "0xabc"
        assert pos.outcome == "YES"
        assert pos.quantity == 50.0
        assert pos.avg_price == 0.55
        # BotState.recent_fills also appended (one new fill on top of
        # the two pre-existing fixture fills).
        fills = list(bot_state.recent_fills)
        assert len(fills) == 3
        injected_fill = fills[-1]
        assert injected_fill.symbol == "0xabc"
        assert injected_fill.action == "UP"
        assert injected_fill.size == 50.0
        assert injected_fill.price == 0.55
        assert injected_fill.strategy == "strategy1"

    async def test_outcome_no_emits_down_action(
        self, test_client, bot_config, bot_state
    ):
        resp = await test_client.post(
            "/debug/inject-position",
            json=_inject_payload(outcome="NO"),
            headers=_auth_headers(bot_config),
        )
        assert resp.status == 200
        fill = list(bot_state.recent_fills)[-1]
        assert fill.action == "DOWN"

    async def test_default_opened_at_uses_now(
        self, test_client, bot_config, bot_state
    ):
        payload = _inject_payload()
        del payload["opened_at"]
        resp = await test_client.post(
            "/debug/inject-position",
            json=payload,
            headers=_auth_headers(bot_config),
        )
        assert resp.status == 200
        pos = bot_state.open_positions["0xabc:YES"]
        # Should be ~now (within 60s).
        now = datetime.now(timezone.utc)
        assert abs((now - pos.opened_at).total_seconds()) < 60

    async def test_live_mode_404(self, test_client, bot_config):
        """Security gate: 404 (not 403, not 401) in non-sim mode so
        the endpoint's existence is not advertised."""
        bot_config.runtime.mode = "live"
        resp = await test_client.post(
            "/debug/inject-position",
            json=_inject_payload(),
            headers=_auth_headers(bot_config),
        )
        assert resp.status == 404

    async def test_live_mode_404_even_without_auth(
        self, test_client, bot_config
    ):
        """404 mode-gate fires BEFORE auth — live deployments should be
        indistinguishable from missing-route 404s for any caller."""
        bot_config.runtime.mode = "live"
        resp = await test_client.post(
            "/debug/inject-position",
            json=_inject_payload(),
        )
        assert resp.status == 404

    async def test_missing_auth_401(self, test_client):
        resp = await test_client.post(
            "/debug/inject-position",
            json=_inject_payload(),
        )
        assert resp.status == 401

    async def test_wrong_token_401(self, test_client):
        resp = await test_client.post(
            "/debug/inject-position",
            json=_inject_payload(),
            headers={"Authorization": "Bearer wrong-token-xyz"},
        )
        assert resp.status == 401

    async def test_invalid_json_400(self, test_client, bot_config):
        resp = await test_client.post(
            "/debug/inject-position",
            data=b"not-json",
            headers={
                "Content-Type": "application/json",
                **_auth_headers(bot_config),
            },
        )
        assert resp.status == 400

    async def test_missing_required_field_400(self, test_client, bot_config):
        payload = _inject_payload()
        del payload["market_id"]
        resp = await test_client.post(
            "/debug/inject-position",
            json=payload,
            headers=_auth_headers(bot_config),
        )
        assert resp.status == 400

    async def test_invalid_outcome_400(self, test_client, bot_config):
        resp = await test_client.post(
            "/debug/inject-position",
            json=_inject_payload(outcome="MAYBE"),
            headers=_auth_headers(bot_config),
        )
        assert resp.status == 400

    async def test_invalid_strategy_400(self, test_client, bot_config):
        resp = await test_client.post(
            "/debug/inject-position",
            json=_inject_payload(strategy="strategy99"),
            headers=_auth_headers(bot_config),
        )
        assert resp.status == 400

    async def test_invalid_opened_at_400(self, test_client, bot_config):
        resp = await test_client.post(
            "/debug/inject-position",
            json=_inject_payload(opened_at="not-iso-8601"),
            headers=_auth_headers(bot_config),
        )
        assert resp.status == 400


# ---------------------------------------------------------------------------
# TestClearPositionsHandler (v0.2.9 3h step 2 companion)
# ---------------------------------------------------------------------------


class TestClearPositionsHandler:
    """POST /debug/clear-positions empties BotState.open_positions and
    BotState.recent_fills. Same security gates as inject."""

    async def test_sim_mode_clears(self, test_client, bot_config, bot_state):
        # Inject first so there's something to clear.
        await test_client.post(
            "/debug/inject-position",
            json=_inject_payload(),
            headers=_auth_headers(bot_config),
        )
        assert "0xabc:YES" in bot_state.open_positions

        resp = await test_client.post(
            "/debug/clear-positions",
            headers=_auth_headers(bot_config),
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["status"] == "cleared"
        assert body["cleared_positions"] >= 1
        assert body["cleared_fills"] >= 1
        # BotState side-effect verified.
        assert bot_state.open_positions == {}
        assert len(bot_state.recent_fills) == 0

    async def test_live_mode_404(self, test_client, bot_config):
        bot_config.runtime.mode = "live"
        resp = await test_client.post(
            "/debug/clear-positions",
            headers=_auth_headers(bot_config),
        )
        assert resp.status == 404

    async def test_missing_auth_401(self, test_client):
        resp = await test_client.post("/debug/clear-positions")
        assert resp.status == 401


# ---------------------------------------------------------------------------
# v0.2.9 B.9 — alerts flow into /dashboard/snapshot (integration)
# ---------------------------------------------------------------------------


class TestSnapshotAlertsAfterSendAlert:
    """v0.2.9 B.9 integration. send_alert → BotState.recent_alerts →
    snapshot body["alerts"]. End-to-end verification that the wiring
    survives the snapshot serialization path."""

    async def test_alert_appears_in_snapshot_alerts(
        self, test_client, bot_config, bot_state
    ) -> None:
        from api.state import set_state
        from utils.alerts import send_alert, set_emitter

        # Register the test BotState so send_alert's _broadcast_dashboard
        # writes into it. Clear the Emitter so we don't need to mock the
        # WS path for this snapshot-side test.
        set_state(bot_state)
        set_emitter(None)
        try:
            await send_alert(
                "smoke alert",
                level="critical",
                source="b9-integration-test",
            )

            resp = await test_client.get(
                "/dashboard/snapshot", headers=_auth_headers(bot_config)
            )
            assert resp.status == 200
            body = await resp.json()
            assert len(body["alerts"]) == 1
            alert = body["alerts"][0]
            # All 5 canonical AlertItem wire fields present in Zod order.
            assert list(alert.keys()) == [
                "id",
                "time",
                "severity",
                "source",
                "message",
            ]
            assert alert["severity"] == "critical"
            assert alert["source"] == "b9-integration-test"
            assert alert["message"] == "smoke alert"
        finally:
            set_state(None)
            set_emitter(None)


# ---------------------------------------------------------------------------
# TestInjectAlertHandler — sim-mode-only b9-fix debug endpoint
# ---------------------------------------------------------------------------


def _inject_alert_payload(**overrides) -> dict:
    base = {
        "message": "smoke test alert",
        "level": "warning",
        "source": "debug-inject-test",
    }
    base.update(overrides)
    return base


class TestInjectAlertHandler:
    """POST /debug/inject-alert. Sim-mode-only debug endpoint that
    fires send_alert internally so the full B.9 production path
    (state.recent_alerts append + WS broadcast) is exercised. Mirrors
    the /debug/inject-position 3h pattern."""

    async def test_sim_mode_200_writes_state_and_returns_alertitem(
        self, test_client, bot_config, bot_state
    ):
        """Happy path: 200 + response body is the 5-field AlertItem +
        BotState.recent_alerts grew by exactly one. The test_client
        fixture wires set_state(bot_state) so send_alert's state-write
        path lights up."""
        resp = await test_client.post(
            "/debug/inject-alert",
            json=_inject_alert_payload(),
            headers=_auth_headers(bot_config),
        )
        assert resp.status == 200
        body = await resp.json()

        # Response is the wire-shape AlertItem in canonical Zod order.
        assert list(body.keys()) == [
            "id",
            "time",
            "severity",
            "source",
            "message",
        ]
        assert body["severity"] == "warning"
        assert body["source"] == "debug-inject-test"
        assert body["message"] == "smoke test alert"

        # State-side append happened (deque started with 0 alerts).
        assert len(bot_state.recent_alerts) == 1
        appended = bot_state.recent_alerts[0]
        assert appended.id == body["id"]
        assert appended.message == body["message"]

    async def test_inject_alert_also_broadcasts_via_ws(
        self, test_client, bot_config, bot_state
    ):
        """The endpoint calls send_alert which BOTH appends to state
        AND broadcasts to WS subscribers. Verify the WS path fires by
        registering an Emitter mock; this completes the b9-fix
        production-parity guarantee."""
        from api.emit import Emitter
        from utils.alerts import set_emitter

        em = MagicMock(spec=Emitter)
        em.broadcast = AsyncMock()
        set_emitter(em)
        try:
            resp = await test_client.post(
                "/debug/inject-alert",
                json=_inject_alert_payload(level="critical"),
                headers=_auth_headers(bot_config),
            )
            assert resp.status == 200
            # State-side: one append.
            assert len(bot_state.recent_alerts) == 1
            # WS-side: one broadcast with matching payload.
            em.broadcast.assert_awaited_once()
            ws_msg = em.broadcast.await_args.args[0]
            assert ws_msg["type"] == "ALERT"
            ws_payload = ws_msg["payload"]
            assert ws_payload["severity"] == "critical"
            assert ws_payload["message"] == "smoke test alert"
            # Same AlertItem flowed to both paths (single source of truth).
            assert ws_payload["id"] == bot_state.recent_alerts[0].id
        finally:
            set_emitter(None)

    async def test_inject_alert_appears_in_subsequent_snapshot(
        self, test_client, bot_config, bot_state
    ):
        """End-to-end runtime-smoke: inject one alert, then GET
        /dashboard/snapshot and assert it appears in body["alerts"]."""
        await test_client.post(
            "/debug/inject-alert",
            json=_inject_alert_payload(message="visible in snapshot"),
            headers=_auth_headers(bot_config),
        )

        resp = await test_client.get(
            "/dashboard/snapshot", headers=_auth_headers(bot_config)
        )
        assert resp.status == 200
        body = await resp.json()
        assert len(body["alerts"]) == 1
        assert body["alerts"][0]["message"] == "visible in snapshot"
        assert body["alerts"][0]["severity"] == "warning"

    @pytest.mark.parametrize("level", ["info", "warning", "critical"])
    async def test_all_severity_literals_accepted(
        self, test_client, bot_config, level, bot_state
    ):
        resp = await test_client.post(
            "/debug/inject-alert",
            json=_inject_alert_payload(level=level),
            headers=_auth_headers(bot_config),
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["severity"] == level

    async def test_live_mode_404_before_auth(self, test_client, bot_config):
        """Security gate ordering: 404 fires BEFORE the auth check —
        live deployments don't acknowledge the endpoint's existence."""
        bot_config.runtime.mode = "live"
        resp = await test_client.post(
            "/debug/inject-alert",
            json=_inject_alert_payload(),
            # Intentionally NO auth header — still 404, not 401, because
            # the mode gate fires first.
        )
        assert resp.status == 404

    async def test_live_mode_404_even_with_auth(self, test_client, bot_config):
        """Same as above but WITH auth — still 404 because mode gate
        fires before the auth check."""
        bot_config.runtime.mode = "live"
        resp = await test_client.post(
            "/debug/inject-alert",
            json=_inject_alert_payload(),
            headers=_auth_headers(bot_config),
        )
        assert resp.status == 404

    async def test_missing_auth_401(self, test_client):
        resp = await test_client.post(
            "/debug/inject-alert",
            json=_inject_alert_payload(),
        )
        assert resp.status == 401

    async def test_wrong_token_401(self, test_client):
        resp = await test_client.post(
            "/debug/inject-alert",
            json=_inject_alert_payload(),
            headers={"Authorization": "Bearer wrong-token-xyz"},
        )
        assert resp.status == 401

    async def test_invalid_json_400(self, test_client, bot_config):
        resp = await test_client.post(
            "/debug/inject-alert",
            data=b"not-json",
            headers={
                "Content-Type": "application/json",
                **_auth_headers(bot_config),
            },
        )
        assert resp.status == 400

    async def test_missing_message_400(self, test_client, bot_config):
        payload = _inject_alert_payload()
        del payload["message"]
        resp = await test_client.post(
            "/debug/inject-alert",
            json=payload,
            headers=_auth_headers(bot_config),
        )
        assert resp.status == 400

    async def test_invalid_level_enum_400(self, test_client, bot_config):
        """level must be Literal["info","warning","critical"];
        anything else (including the internal 5-tier "error") fails
        Pydantic validation with 400."""
        resp = await test_client.post(
            "/debug/inject-alert",
            json=_inject_alert_payload(level="error"),
            headers=_auth_headers(bot_config),
        )
        assert resp.status == 400

    async def test_empty_message_400(self, test_client, bot_config):
        resp = await test_client.post(
            "/debug/inject-alert",
            json=_inject_alert_payload(message=""),
            headers=_auth_headers(bot_config),
        )
        assert resp.status == 400


# ---------------------------------------------------------------------------
# TestInjectLogHandler — sim-mode-only B.8 debug endpoint
# ---------------------------------------------------------------------------


def _inject_log_payload(**overrides) -> dict:
    base = {
        "message": "smoke test log",
        "level": "info",
        "source": "debug-inject-test",
    }
    base.update(overrides)
    return base


class TestInjectLogHandler:
    """POST /debug/inject-log. Sim-mode-only debug endpoint that
    fires utils.logs.log_to_dashboard internally so the full B.8
    production path (state.recent_logs append; 3f Option C lossy
    level map applied) is exercised. Mirrors inject-alert (b9-fix
    pattern)."""

    async def test_sim_mode_200_writes_state_and_returns_botlogentry(
        self, test_client, bot_config, bot_state
    ):
        resp = await test_client.post(
            "/debug/inject-log",
            json=_inject_log_payload(),
            headers=_auth_headers(bot_config),
        )
        assert resp.status == 200
        body = await resp.json()

        # Response is the wire-shape BotLogEntry in canonical Zod order.
        assert list(body.keys()) == ["id", "time", "level", "message"]
        assert body["level"] == "INFO"  # 3f Option C: info → "INFO"
        # source folded into message (BotLogEntry has no source field).
        assert body["message"] == "[debug-inject-test] smoke test log"

        # State-side append happened.
        assert len(bot_state.recent_logs) == 1
        appended = bot_state.recent_logs[0]
        assert appended.id == body["id"]

    async def test_debug_level_returns_204_no_content(
        self, test_client, bot_config, bot_state
    ):
        """3f Option C: debug-tier entries are dropped from the
        dashboard. Endpoint returns 204 No Content (intentional drop)
        rather than 200 + empty body or 500 (which would imply error).
        BotState.recent_logs MUST NOT grow."""
        resp = await test_client.post(
            "/debug/inject-log",
            json=_inject_log_payload(level="debug"),
            headers=_auth_headers(bot_config),
        )
        assert resp.status == 204
        assert len(bot_state.recent_logs) == 0

    async def test_critical_collapses_to_ERROR(
        self, test_client, bot_config, bot_state
    ):
        """3f Option C: critical → ERROR (no higher wire tier).
        Endpoint preserves this contract — caller passing
        level='critical' sees the wire entry with level='ERROR'."""
        resp = await test_client.post(
            "/debug/inject-log",
            json=_inject_log_payload(level="critical"),
            headers=_auth_headers(bot_config),
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["level"] == "ERROR"

    @pytest.mark.parametrize(
        "internal_level,expected_wire",
        [
            ("info", "INFO"),
            ("warning", "WARN"),
            ("error", "ERROR"),
            ("critical", "ERROR"),
        ],
    )
    async def test_all_non_debug_levels_round_trip(
        self,
        test_client,
        bot_config,
        bot_state,
        internal_level,
        expected_wire,
    ):
        """The full 3f Option C non-drop subset (4 internal → 3 wire
        values, with critical collapsing onto ERROR)."""
        resp = await test_client.post(
            "/debug/inject-log",
            json=_inject_log_payload(level=internal_level),
            headers=_auth_headers(bot_config),
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["level"] == expected_wire

    async def test_inject_log_appears_in_subsequent_snapshot(
        self, test_client, bot_config, bot_state
    ):
        """End-to-end runtime-smoke: inject one log, then GET
        /dashboard/snapshot and assert it appears in body['logs']."""
        await test_client.post(
            "/debug/inject-log",
            json=_inject_log_payload(message="visible in snapshot"),
            headers=_auth_headers(bot_config),
        )

        resp = await test_client.get(
            "/dashboard/snapshot", headers=_auth_headers(bot_config)
        )
        assert resp.status == 200
        body = await resp.json()
        assert len(body["logs"]) == 1
        assert body["logs"][0]["message"] == "[debug-inject-test] visible in snapshot"
        assert body["logs"][0]["level"] == "INFO"

    async def test_live_mode_404_before_auth(self, test_client, bot_config):
        bot_config.runtime.mode = "live"
        resp = await test_client.post(
            "/debug/inject-log",
            json=_inject_log_payload(),
            # No auth header — still 404 because the mode gate fires first.
        )
        assert resp.status == 404

    async def test_live_mode_404_even_with_auth(self, test_client, bot_config):
        bot_config.runtime.mode = "live"
        resp = await test_client.post(
            "/debug/inject-log",
            json=_inject_log_payload(),
            headers=_auth_headers(bot_config),
        )
        assert resp.status == 404

    async def test_missing_auth_401(self, test_client):
        resp = await test_client.post(
            "/debug/inject-log",
            json=_inject_log_payload(),
        )
        assert resp.status == 401

    async def test_wrong_token_401(self, test_client):
        resp = await test_client.post(
            "/debug/inject-log",
            json=_inject_log_payload(),
            headers={"Authorization": "Bearer wrong-token-xyz"},
        )
        assert resp.status == 401

    async def test_invalid_json_400(self, test_client, bot_config):
        resp = await test_client.post(
            "/debug/inject-log",
            data=b"not-json",
            headers={
                "Content-Type": "application/json",
                **_auth_headers(bot_config),
            },
        )
        assert resp.status == 400

    async def test_missing_message_400(self, test_client, bot_config):
        payload = _inject_log_payload()
        del payload["message"]
        resp = await test_client.post(
            "/debug/inject-log",
            json=payload,
            headers=_auth_headers(bot_config),
        )
        assert resp.status == 400

    async def test_invalid_level_enum_400(self, test_client, bot_config):
        """level must be Literal["debug","info","warning","error",
        "critical"] (internal 5-tier). Wire-side uppercase ("INFO") or
        unknown ("trace") fails Pydantic validation with 400."""
        for invalid in ("INFO", "WARN", "trace", "fatal"):
            resp = await test_client.post(
                "/debug/inject-log",
                json=_inject_log_payload(level=invalid),
                headers=_auth_headers(bot_config),
            )
            assert resp.status == 400, f"level={invalid!r} should be 400"

    async def test_empty_message_400(self, test_client, bot_config):
        resp = await test_client.post(
            "/debug/inject-log",
            json=_inject_log_payload(message=""),
            headers=_auth_headers(bot_config),
        )
        assert resp.status == 400


# ---------------------------------------------------------------------------
# v0.2.9 B.7 — equity flow into /dashboard/snapshot (integration)
# ---------------------------------------------------------------------------


class TestSnapshotEquityHistory:
    """v0.2.9 B.7 integration. EquityPoints written to
    BotState.equity_history (production: by
    utils.equity_curve.equity_snapshot_loop; test: by direct
    fixture-side manipulation) flow through the snapshot to
    body["equity"]. End-to-end verification that the snapshot path is
    actually live (no longer the OQ-3 stub)."""

    async def test_equity_history_appears_in_snapshot(
        self, test_client, bot_config, bot_state
    ) -> None:
        # Inject directly into the fixture's BotState. The production
        # path uses the equity_snapshot_loop background task; this test
        # focuses on the snapshot serialization, not the loop's wiring
        # (which is covered in tests/utils/test_equity_curve.py).
        from api.schemas import EquityPoint

        bot_state.equity_history.append(
            EquityPoint(day="2026-05-19", equity=9500.0, pnl=-500.0)
        )
        bot_state.equity_history.append(
            EquityPoint(day="2026-05-20", equity=10000.0, pnl=0.0)
        )
        bot_state.equity_history.append(
            EquityPoint(day="2026-05-21", equity=10250.0, pnl=250.0)
        )

        resp = await test_client.get(
            "/dashboard/snapshot", headers=_auth_headers(bot_config)
        )
        assert resp.status == 200
        body = await resp.json()

        assert len(body["equity"]) == 3
        # Canonical 3-field shape per 3f (day, equity, pnl in Zod order).
        for point in body["equity"]:
            assert list(point.keys()) == ["day", "equity", "pnl"]

        # First → middle → last preserves deque insertion order.
        assert body["equity"][0]["day"] == "2026-05-19"
        assert body["equity"][0]["equity"] == 9500.0
        assert body["equity"][0]["pnl"] == -500.0
        assert body["equity"][2]["day"] == "2026-05-21"
        assert body["equity"][2]["equity"] == 10250.0
        assert body["equity"][2]["pnl"] == 250.0

    async def test_empty_equity_history_serializes_as_empty_array(
        self, test_client, bot_config, bot_state
    ) -> None:
        """Pre-bootstrap (or fresh BotState in tests): equity_history
        is empty → body["equity"] is []. Must serialize cleanly, not
        as null."""
        assert len(bot_state.equity_history) == 0

        resp = await test_client.get(
            "/dashboard/snapshot", headers=_auth_headers(bot_config)
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["equity"] == []
        assert isinstance(body["equity"], list)


# ---------------------------------------------------------------------------
# v0.2.9 B.1 — RiskMetrics computed at snapshot-build time (PATH C)
# ---------------------------------------------------------------------------


class TestSnapshotRiskMetricsAfterB1Wiring:
    """v0.2.9 B.1: _build_riskmetrics orchestrates the 5 pure-function
    helpers from utils/risk_metrics.py. Verify end-to-end via the
    snapshot path that previously-null fields now carry real values
    when sufficient input data is present, AND degrade cleanly to null
    on insufficient data.

    Note: bot_config is a MagicMock by default — config.risk and
    config.risk.daily_drawdown_limit_pct auto-create as MagicMock
    instances. For liquidation-risk-specific assertions, individual
    tests set the value explicitly.
    """

    async def test_empty_state_yields_null_except_leverage(
        self, test_client, bot_config, bot_state
    ):
        """Fresh BotState (no equity_history, no positions, fresh equity):
        - maxDrawdown: null (need ≥2 points)
        - sharpe: null (need ≥20 returns)
        - var95: null (need ≥21 returns)
        - leverage: 1.0 (3e structural constant)
        - exposurePct: 0.0 (empty positions, positive equity)
        - liquidationRisk: 'low' (no intra-day drawdown)
        """
        # Set the daily_drawdown_limit_pct on the MagicMock so the
        # liquidation-risk helper has a real number to compare.
        bot_config.risk.daily_drawdown_limit_pct = 0.12

        resp = await test_client.get(
            "/dashboard/snapshot", headers=_auth_headers(bot_config)
        )
        assert resp.status == 200
        body = await resp.json()
        risk = body["risk"]
        # Wire order must remain canonical (lock; verified per 3e).
        assert list(risk.keys()) == [
            "maxDrawdown",
            "sharpe",
            "var95",
            "leverage",
            "exposurePct",
            "liquidationRisk",
        ]
        assert risk["maxDrawdown"] is None
        assert risk["sharpe"] is None
        assert risk["var95"] is None
        assert risk["leverage"] == 1.0
        assert risk["exposurePct"] == 0.0
        # No intra-day drawdown (equity == day_start_equity from fixture).
        assert risk["liquidationRisk"] == "low"

    async def test_populated_state_yields_real_values(
        self, test_client, bot_config, bot_state
    ):
        """Synthetic equity_history with ≥22 points + open positions →
        all 5 computed fields show non-null values. Verifies wire-shape
        contracts (percent for drawdown/exposure, USD for var95,
        enum for liquidationRisk)."""
        from api.schemas import EquityPoint
        from utils.types import Position

        bot_config.risk.daily_drawdown_limit_pct = 0.12

        # Build 22 daily points: mix of small gains and losses so
        # sharpe + var95 + maxDrawdown all have material data.
        equities = [10000.0]
        deltas = [
            0.005, -0.01, 0.008, -0.015, 0.012, -0.02, 0.003,
            -0.005, 0.01, -0.008, 0.005, -0.012, 0.007, -0.01,
            0.004, -0.02, 0.011, -0.008, 0.006, -0.015, 0.009,
        ]
        for d in deltas:
            equities.append(equities[-1] * (1.0 + d))

        for i, eq in enumerate(equities):
            bot_state.equity_history.append(
                EquityPoint(day=f"2026-05-{i+1:02d}", equity=eq, pnl=0.0)
            )

        # Add two open positions for exposurePct.
        bot_state.open_positions["k1"] = Position(
            market_id="0xabc",
            outcome="YES",
            quantity=100.0,
            avg_price=0.55,
            strategy="intra_arbitrage",
        )
        bot_state.open_positions["k2"] = Position(
            market_id="0xdef",
            outcome="NO",
            quantity=50.0,
            avg_price=0.40,
            strategy="momentum_latency",
        )

        # Adjust bot_state.equity to introduce a small intra-day
        # drawdown so liquidationRisk has a non-trivial value to check.
        # day_start_equity=9500 from the fixture; set equity below to
        # produce ~5% dd (well under the 12% limit → "low").
        bot_state.equity = 9100.0  # 4.2% dd from day_start 9500

        resp = await test_client.get(
            "/dashboard/snapshot", headers=_auth_headers(bot_config)
        )
        assert resp.status == 200
        body = await resp.json()
        risk = body["risk"]

        # leverage is unchanged.
        assert risk["leverage"] == 1.0

        # maxDrawdown is a positive percent (lock 1 + 2).
        assert risk["maxDrawdown"] is not None
        assert risk["maxDrawdown"] > 0.0
        assert isinstance(risk["maxDrawdown"], float)

        # sharpe is finite (could be positive or negative).
        assert risk["sharpe"] is not None
        assert isinstance(risk["sharpe"], float)

        # var95 is a positive USD figure (lock 10).
        assert risk["var95"] is not None
        assert risk["var95"] > 0.0
        assert isinstance(risk["var95"], float)

        # exposurePct: (100*0.55 + 50*0.40) / 9100 * 100 = 75/9100 * 100 ≈ 0.824
        assert risk["exposurePct"] is not None
        assert risk["exposurePct"] == pytest.approx(75.0 / 9100.0 * 100.0)

        # liquidationRisk: ~4.2% dd vs 12% limit → 35% of limit → "low".
        assert risk["liquidationRisk"] == "low"

    async def test_high_drawdown_yields_high_liquidation_risk(
        self, test_client, bot_config, bot_state
    ):
        """Push equity deep enough that intra-day drawdown ≥ 80% of
        daily_drawdown_limit_pct → liquidationRisk = 'high'."""
        bot_config.risk.daily_drawdown_limit_pct = 0.12
        # day_start 9500 → set equity to 8500 = ~10.5% dd → 87% of limit → "high".
        bot_state.equity = 8500.0

        resp = await test_client.get(
            "/dashboard/snapshot", headers=_auth_headers(bot_config)
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["risk"]["liquidationRisk"] == "high"


# ---------------------------------------------------------------------------
# v0.2.9 v029-monthly-pnl-wiring — monthlyPnl flows through snapshot
# ---------------------------------------------------------------------------


class TestSnapshotMonthlyPnlAfterWiring:
    """End-to-end: utils.equity_curve.compute_monthly_pnl runs at
    snapshot-build time, grouping BotState.equity_history by month
    prefix and emitting wire MonthlyPnl rows. Verify shape (2 fields
    only — no `trades`), order (ascending), and PnL semantics
    (last - first per month)."""

    async def test_two_months_two_rows_ascending(
        self, test_client, bot_config, bot_state
    ) -> None:
        from api.schemas import EquityPoint

        # 5 points spanning April + May 2026 (per coordinator scope).
        # April: 3 points → qualifies. May: 2 points → qualifies.
        bot_state.equity_history.append(EquityPoint(day="2026-04-01", equity=9500.0, pnl=0.0))
        bot_state.equity_history.append(EquityPoint(day="2026-04-15", equity=9800.0, pnl=0.0))
        bot_state.equity_history.append(EquityPoint(day="2026-04-30", equity=10100.0, pnl=0.0))
        bot_state.equity_history.append(EquityPoint(day="2026-05-01", equity=10100.0, pnl=0.0))
        bot_state.equity_history.append(EquityPoint(day="2026-05-20", equity=10500.0, pnl=0.0))

        resp = await test_client.get(
            "/dashboard/snapshot", headers=_auth_headers(bot_config)
        )
        assert resp.status == 200
        body = await resp.json()

        # Two rows, ascending by month string.
        assert len(body["monthlyPnl"]) == 2
        assert body["monthlyPnl"][0]["month"] == "2026-04"
        assert body["monthlyPnl"][1]["month"] == "2026-05"

        # PnL = last - first within each month.
        # April: 10100 - 9500 = 600
        assert body["monthlyPnl"][0]["pnl"] == pytest.approx(600.0)
        # May: 10500 - 10100 = 400
        assert body["monthlyPnl"][1]["pnl"] == pytest.approx(400.0)

        # Wire shape: exactly 2 keys per row, ZERO `trades` field.
        for row in body["monthlyPnl"]:
            assert list(row.keys()) == ["month", "pnl"]
            assert "trades" not in row

    async def test_empty_equity_history_serializes_empty_list(
        self, test_client, bot_config, bot_state
    ) -> None:
        """Pre-bootstrap or fresh BotState: equity_history empty →
        body["monthlyPnl"] = []. Mirrors the analogous empty-equity_history
        assertion for the equity slice."""
        assert len(bot_state.equity_history) == 0

        resp = await test_client.get(
            "/dashboard/snapshot", headers=_auth_headers(bot_config)
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["monthlyPnl"] == []
        assert isinstance(body["monthlyPnl"], list)
