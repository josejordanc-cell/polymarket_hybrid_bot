"""
api/routes_rest.py — Three REST handlers for the bot HTTP module.

Implements D.2.2 Step 4 per docs/architecture/D.2.1-http-module.md §D-3:

    GET  /dashboard/snapshot      — full bot state per DashboardSnapshot
    POST /api/strategies/config   — runtime strategy config patch
    POST /emergency-stop          — sets kill_switch_requested on BotState

All three are Bearer-authenticated via api.auth.verify_bearer. The
snapshot handler acquires state.write_lock briefly to call
state.shallow_copy(), then releases before serialising (D.2.1 §D-4).
The two write handlers acquire state.write_lock for the mutation only.

WebSocket handlers and message emission are deferred to D.2.3 — this
file deliberately contains no WS-shaped code.
"""
from __future__ import annotations

import time
from typing import Any

import structlog
from aiohttp import web
from pydantic import ValidationError

from api.auth import verify_bearer
from api.schemas import (
    DashboardSnapshot,
    GlobalStats,
    StopResponse,
    StrategyCardData,
    StrategyConfigPatch,
)
from api.state import BotState

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Snapshot serialisation helper
# ---------------------------------------------------------------------------


def _build_snapshot(snap: BotState) -> DashboardSnapshot:
    """Build a DashboardSnapshot from a shallow-copy BotState.

    Pure function — does not touch the original BotState or any lock.
    OQ-3 fields (positions, history, logs, alerts, metrics) are stub
    empties until D.2.4 wires telemetry.
    """
    day_pnl = snap.equity - snap.day_start_equity

    strategy_name_map = {
        "strategy1": "Intra-Arbitrage",
        "strategy2": "Spot-Lag Momentum",
        "strategy3": "Market Maker",
    }
    pipeline_step_map = {"strategy1": 1, "strategy2": 2, "strategy3": 3}

    return DashboardSnapshot(
        globalStats=GlobalStats(
            portfolioValue=snap.equity,
            pnlToday=day_pnl,
            pnlMonth=0.0,            # OQ-3 stub — pipeline not wired
            pnlAllTime=0.0,          # OQ-3 stub — pipeline not wired
            winRate=0.0,             # OQ-3 stub — pipeline not wired
            totalTrades=0,           # OQ-3 stub — pipeline not wired
            streak=0,                # OQ-3 stub — pipeline not wired
            lastUpdatedAt=snap.last_updated_at_ms,
        ),
        strategies=[
            StrategyCardData(
                id=cfg.strategyId,
                name=strategy_name_map[cfg.strategyId],
                status="live" if cfg.enabled else "paused",
                # Telemetry fields default to None / [] until OQ-3 wiring
                # tracks per-strategy pnl, trades, hold time, signal,
                # exposure, and sparkline. See StrategyCardData docstring
                # and d24-emit-site-audit.md §10 (STRATEGY_UPDATE drift).
                # StrategyCardData.id stays "id" per audit row B.2 (ALIGNED);
                # only StrategyConfig/Patch/RuntimeConfig get the
                # id→strategyId rename per audit row B.6 + L2 E.2.
                pipelineStep=pipeline_step_map[cfg.strategyId],
            )
            for cfg in (
                snap.strategy_configs[k]
                for k in ("strategy1", "strategy2", "strategy3")
                if k in snap.strategy_configs
            )
        ],
        strategyConfigs=[
            cfg.to_contract()
            for cfg in (
                snap.strategy_configs[k]
                for k in ("strategy1", "strategy2", "strategy3")
                if k in snap.strategy_configs
            )
        ],
        # v0.2.9 audit row A.1: six top-level renames. BotState snake_case
        # attributes (snap.open_positions, snap.recent_fills, etc.) are
        # internal — only the wire-facing kwargs here changed.
        positions=[],       # OQ-3 stub
        fills=list(snap.recent_fills),
        equity=[],          # OQ-3 stub
        monthlyPnl=[],      # OQ-3 stub
        backtest=[],        # OQ-3 stub
        logs=[],            # OQ-3 stub
        alerts=[],          # OQ-3 stub
    )


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def snapshot_handler(request: web.Request, state: BotState, config: Any) -> web.Response:
    if not verify_bearer(request.headers, config):
        return web.Response(status=401, text="Unauthorized")
    async with state.write_lock:
        snap = state.shallow_copy()
    # Build DashboardSnapshot from snap OUTSIDE the lock (see D.2.1 §D-4)
    snapshot = _build_snapshot(snap)
    logger.info("snapshot_served", last_updated_at_ms=snap.last_updated_at_ms)
    return web.json_response(snapshot.model_dump(mode="json"))


async def strategies_config_handler(request: web.Request, state: BotState, config: Any) -> web.Response:
    if not verify_bearer(request.headers, config):
        return web.Response(status=401, text="Unauthorized")
    try:
        body = await request.json()
    except Exception:
        return web.Response(status=400, text="Invalid JSON")
    try:
        patch = StrategyConfigPatch.model_validate(body)
    except ValidationError as exc:
        return web.Response(status=422, text=str(exc))
    async with state.write_lock:
        state.apply_strategy_patch(patch)
    logger.info("strategy_config_patched", strategy_id=patch.strategyId)
    return web.json_response({"ok": True})


async def emergency_stop_handler(request: web.Request, state: BotState, config: Any) -> web.Response:
    if not verify_bearer(request.headers, config):
        return web.Response(status=401, text="Unauthorized")
    stopped_at = int(time.time() * 1000)
    async with state.write_lock:
        state.kill_switch_requested = True
    logger.info("kill_switch_set", stopped_at_ms=stopped_at)
    return web.json_response(
        StopResponse(ok=True, stoppedAt=stopped_at).model_dump(mode="json")
    )


# ---------------------------------------------------------------------------
# Closure factory + route registration
# ---------------------------------------------------------------------------


def _make_handler(fn: Any, state: BotState, config: Any) -> Any:
    async def _handler(request: web.Request) -> web.Response:
        return await fn(request, state, config)
    return _handler


def setup_routes(app: web.Application, state: BotState, config: Any) -> None:
    """Register the three D.2.2 REST routes onto an aiohttp Application."""
    app.router.add_get(
        "/dashboard/snapshot",
        _make_handler(snapshot_handler, state, config),
    )
    app.router.add_post(
        "/api/strategies/config",
        _make_handler(strategies_config_handler, state, config),
    )
    app.router.add_post(
        "/emergency-stop",
        _make_handler(emergency_stop_handler, state, config),
    )
