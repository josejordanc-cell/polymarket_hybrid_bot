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
import uuid
from datetime import datetime, timezone
from typing import Any, Literal, Optional

import structlog
from aiohttp import web
from pydantic import BaseModel, Field, ValidationError, field_validator

from api.auth import verify_bearer
from api.schemas import (
    DashboardSnapshot,
    FillRecord,
    GlobalStats,
    OpenPosition,
    RiskMetrics,
    StopResponse,
    StrategyCardData,
    StrategyConfigPatch,
    StrategyId,
)
from api.state import BotState
from utils.equity_curve import compute_monthly_pnl
from utils.types import Position

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Wire-shape helpers
# ---------------------------------------------------------------------------


# v0.2.9 B.8 wiring: the log-level map + _map_log_level helper that
# used to live here (from 3f speculative landing) have moved to
# ``utils/logs.py`` alongside ``log_to_dashboard`` — their natural
# home now that the broadcast site exists. Single source of truth.
# New code imports directly from utils.logs; tests for the helper
# live next to its new module (tests/utils/test_logs.py).


# v0.2.9 3f (E.3 collapse, Jose 2026-05-21): internal→wire alert-severity
# map. Same drop-debug / collapse-error pattern as the log-level map.
_ALERT_SEVERITY_MAP: dict[str, Optional[str]] = {
    "debug": None,           # drop debug alerts from the wire
    "info": "info",
    "warning": "warning",
    "error": "critical",     # E.3 collapse to highest wire tier
    "critical": "critical",
}


def _map_alert_severity(internal: str) -> Optional[str]:
    """Map an internal alert severity (lowercase) to the wire AlertItem
    ``severity`` enum literal, or ``None`` to drop the entry.

    Raises ``ValueError`` on unknown severities — same defensive policy
    as ``_map_log_level``.
    """
    try:
        return _ALERT_SEVERITY_MAP[internal]
    except KeyError as exc:
        raise ValueError(
            f"Unknown internal alert severity {internal!r}; "
            f"expected one of {sorted(_ALERT_SEVERITY_MAP.keys())!r}"
        ) from exc


def _build_open_position(pos: Any) -> OpenPosition:
    """Build a wire OpenPosition from an internal ``utils.types.Position``.

    Implements the locked-policy mapping from v0.2.9 3d-fix (see
    ``api/schemas.py:OpenPosition`` docstring + Decision Log entry
    ``v029-3d-fix``). All four stopgap fields (``tradeId``,
    ``currentPrice``, ``unrealizedPnlPct``, ``unrealizedPnlUsd``) are
    marked ``TODO(B.3-wiring)``; they ship deterministic stopgap values
    until the RiskManager→BotState mark-pricing pipeline lands in B.1
    full-wiring.

    Accepts ``Any`` rather than ``Position`` to match the dict-or-object
    flexibility of ``_collapse_position_id`` and to keep this module
    layer-light; the locked-policy field reads still assume
    ``utils.types.Position`` semantics.
    """
    pos_id = _collapse_position_id(pos)
    time_open = (
        pos.opened_at
        .astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )
    direction = "Long" if pos.outcome == "YES" else "Short"
    return OpenPosition(
        id=pos_id,
        tradeId=pos_id,                              # TODO(B.3-wiring): stopgap, internal Position has no per-fill id
        symbol=pos.market_id[:12],                   # display-only truncate
        strategy=pos.strategy,
        direction=direction,
        entryPrice=pos.avg_price,
        currentPrice=pos.avg_price,                  # TODO(B.3-wiring): stopgap, no mark-pricing surface yet
        size=pos.quantity,
        unrealizedPnlPct=0.0,                        # TODO(B.3-wiring): stopgap
        unrealizedPnlUsd=0.0,                        # TODO(B.3-wiring): stopgap
        timeOpen=time_open,
    )


def _collapse_position_id(pos: Any) -> str:
    """Collapse an internal position into the single ``id`` string the
    dashboard's wire OpenPosition expects (v0.2.9 audit row B.3 + L2 E.1).

    Two patterns are supported by probing for attributes-or-dict-keys:

    - Polymarket: ``id = f"{marketId}:{outcome}"`` (preferred when the
      input exposes both fields; matches the bot's internal
      ``utils.types.Position`` shape which uses snake_case
      ``market_id``/``outcome`` — both spellings are accepted).
    - Spot venue: ``id = f"{venue}:{symbol}"`` (e.g.
      ``"binance:BTC/USDT"``).

    Raises ``ValueError`` if neither pattern matches — surfaces unknown
    position shapes at the boundary rather than silently corrupting the
    wire id.
    """
    def _get(obj: Any, key: str) -> Any:
        if isinstance(obj, dict):
            return obj.get(key)
        return getattr(obj, key, None)

    # Polymarket pattern — accept camelCase (wire) and snake_case (internal).
    market_id = _get(pos, "marketId") or _get(pos, "market_id")
    outcome = _get(pos, "outcome")
    if market_id is not None and outcome is not None:
        return f"{market_id}:{outcome}"

    # Spot pattern.
    venue = _get(pos, "venue")
    symbol = _get(pos, "symbol")
    if venue is not None and symbol is not None:
        return f"{venue}:{symbol}"

    raise ValueError(
        "Cannot collapse position id: input must expose either "
        "(marketId|market_id + outcome) for Polymarket positions or "
        "(venue + symbol) for spot positions."
    )


# ---------------------------------------------------------------------------
# Snapshot serialisation helper
# ---------------------------------------------------------------------------


def _build_riskmetrics(snap: BotState, config: Any) -> RiskMetrics:
    """Build a wire RiskMetrics from a snapshot + config (v0.2.9 B.1).

    Orchestrates the five pure-function helpers in ``utils.risk_metrics``
    plus the spot-venue ``leverage = 1.0`` structural constant from 3e.
    Reads ``config.risk.daily_drawdown_limit_pct`` defensively via a
    getattr chain so a partial config / MagicMock without a ``risk``
    attribute degrades gracefully (``liquidationRisk = None`` rather
    than raising).

    All five helpers return ``None`` cleanly on degenerate input
    (empty equity_history, insufficient samples, zero equity). The
    ``RiskMetrics`` Pydantic accepts ``None`` for the five nullable
    fields per 3e canonical shape.
    """
    from utils.risk_metrics import (
        compute_exposure_pct,
        compute_liquidation_risk,
        compute_max_drawdown,
        compute_sharpe,
        compute_var95_usd,
    )

    # daily_drawdown_limit_pct lives at cfg.risk.daily_drawdown_limit_pct
    # (config/settings.py:161). Defensive getattr chain: a partial config
    # without .risk falls through to None → liquidationRisk is None.
    # The isinstance check below is also defense-in-depth: a bare
    # MagicMock without spec auto-vivifies any attribute as a MagicMock
    # (not a float), and the threshold comparison inside the helper
    # would raise TypeError. The 3h-fix lesson applies here too.
    risk_cfg = getattr(config, "risk", None)
    daily_limit_frac = getattr(risk_cfg, "daily_drawdown_limit_pct", None)

    liquidation_risk = (
        compute_liquidation_risk(
            day_start_equity=snap.day_start_equity,
            equity=snap.equity,
            daily_drawdown_limit_pct=daily_limit_frac,
        )
        if isinstance(daily_limit_frac, (int, float))
        else None
    )

    return RiskMetrics(
        maxDrawdown=compute_max_drawdown(snap.equity_history),
        sharpe=compute_sharpe(snap.equity_history),
        var95=compute_var95_usd(snap.equity_history, snap.equity),
        leverage=1.0,  # 3e structural constant: Polymarket spot is non-leveraged
        exposurePct=compute_exposure_pct(snap.open_positions, snap.equity),
        liquidationRisk=liquidation_risk,
    )


def _build_snapshot(snap: BotState, config: Any) -> DashboardSnapshot:
    """Build a DashboardSnapshot from a shallow-copy BotState + config.

    Pure function — does not touch the original BotState or any lock.
    ``config`` is read for the B.1 RiskMetrics computation
    (``cfg.risk.daily_drawdown_limit_pct``); other slices read from
    ``snap`` only.
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
            # v0.2.9 3d-pre fix: ship live epoch ms, not snap.last_updated_at_ms
            # (which sits at 0 until a fill/heartbeat lands). Dashboard's
            # refreshDashboard.ts stale-snapshot gate (BOT_CONTRACT.md v1.0
            # line 186) compares incomingTs <= storedTs; a literal 0
            # always loses against the portfolioStore seed (Date.now()),
            # causing every snapshot to merge-as-stale → silent mock
            # fallback. int(time.time() * 1000) satisfies the monotonic-
            # increasing requirement at snapshot build time.
            lastUpdatedAt=int(time.time() * 1000),
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
            # v0.2.9 3d-pre fix: activePairs ships [] not null when no
            # pairs are configured. Dashboard Zod (BOT_CONTRACT.md v1.0
            # line 121) requires z.array(...) NOT nullable — null caused
            # every config row to be silently rejected. Fix at the
            # serializer site by overriding activePairs to [] when the
            # source is None. StrategyRuntimeConfig.to_contract() still
            # propagates None for internal callers; only the wire
            # representation is coerced.
            cfg.to_contract().model_copy(
                update={"activePairs": cfg.activePairs or []}
            )
            for cfg in (
                snap.strategy_configs[k]
                for k in ("strategy1", "strategy2", "strategy3")
                if k in snap.strategy_configs
            )
        ],
        # v0.2.9 audit row A.1: six top-level renames. BotState snake_case
        # attributes (snap.open_positions, snap.recent_fills, etc.) are
        # internal — only the wire-facing kwargs here changed.
        #
        # positions: v0.2.9 3h wires this live. Each value in
        # snap.open_positions is an internal utils.types.Position;
        # _build_open_position() applies the locked-policy 11-field
        # mapping (3d-fix). Four stopgap fields (tradeId, currentPrice,
        # unrealizedPnl{Pct,Usd}) remain TODO(B.3-wiring) until the
        # RiskManager→BotState mark-pricing pipeline lands. Production
        # fill-flow refresh is also B.1-full-wiring; until then this
        # path is exercised by the /debug/inject-position endpoint
        # (sim-mode-only, see inject_position_handler below).
        positions=[
            _build_open_position(p) for p in snap.open_positions.values()
        ],
        fills=list(snap.recent_fills),
        # v0.2.9 B.1: snapshot-build-time RiskMetrics computation
        # (PATH C). _build_riskmetrics orchestrates five pure-function
        # helpers in utils.risk_metrics + the 3e leverage=1.0 structural
        # constant. All five computed fields degrade to None when the
        # snapshot lacks sufficient samples or has degenerate values
        # (zero equity, etc.). See .swarm-decisions.md v029-b1-risk-wiring
        # for the 11 coordinator-locked decisions.
        risk=_build_riskmetrics(snap, config),
        # equity: v0.2.9 B.7 wires this live. Wire-shape EquityPoint
        # instances are appended to BotState.equity_history by
        # utils.equity_curve.equity_snapshot_loop — a long-lived
        # background task started in api/server.py that bootstraps
        # today's point on bot start and polls hourly for UTC-day
        # rollovers (one point per UTC calendar day, capped at
        # maxlen=200 ~ 6.5 months of daily-close history). Snapshot
        # just lists the deque; mirrors the fills= / alerts= / logs=
        # patterns.
        equity=list(snap.equity_history),
        # v0.2.9 v029-monthly-pnl-wiring: aggregate equity_history into
        # per-month PnL rows. Helper groups by "YYYY-MM" month prefix,
        # emits one MonthlyPnl per month with ≥2 points, sorted ascending.
        # Lazy snapshot-build-time computation (PATH A) — mirrors B.1's
        # _build_riskmetrics pattern. Returns [] for empty equity_history.
        monthlyPnl=compute_monthly_pnl(snap.equity_history),
        backtest=[],        # OQ-3 stub
        # logs: v0.2.9 B.8 wires this live. Wire-shape BotLogEntry
        # instances are appended to BotState.recent_logs by
        # utils.logs.log_to_dashboard (the explicit-opt-in helper —
        # PATH A per the B.8 design decision). The 5→4 tier lossy
        # map (3f Option C) is applied INSIDE log_to_dashboard, so
        # everything reaching recent_logs is already wire-canonical;
        # the snapshot just lists the deque. Mirrors the
        # fills=list(snap.recent_fills) and
        # alerts=list(snap.recent_alerts) patterns.
        logs=list(snap.recent_logs),
        # alerts: v0.2.9 B.9 wires this live. Wire-shape AlertItem
        # instances are appended to BotState.recent_alerts by
        # utils.alerts._broadcast_dashboard (see send_alert path), so
        # the snapshot just lists the deque. Mirrors the
        # fills=list(snap.recent_fills) pattern. The internal-to-wire
        # severity collapse (E.3) is handled by callers BEFORE the
        # AlertItem is constructed (or by the implicit Pydantic
        # Literal constraint at construction time); see
        # _map_alert_severity in this module for the helper that
        # callers can use when their source is an internal 5-tier.
        alerts=list(snap.recent_alerts),
    )


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def snapshot_handler(request: web.Request, state: BotState, config: Any) -> web.Response:
    if not verify_bearer(request.headers, config):
        return web.Response(status=401, text="Unauthorized")
    async with state.write_lock:
        snap = state.shallow_copy()
    # Build DashboardSnapshot from snap OUTSIDE the lock (see D.2.1 §D-4).
    # v0.2.9 B.1: config threaded through for RiskMetrics computation
    # (read of cfg.risk.daily_drawdown_limit_pct for liquidationRisk).
    snapshot = _build_snapshot(snap, config)
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
# Debug-injection endpoints (sim-mode only; v0.2.9 3h)
# ---------------------------------------------------------------------------
#
# These endpoints exist to exercise the snapshot-side wire shape
# (OpenPosition 11-field + FillRecord 9-field) end-to-end without
# requiring the real RiskManager→BotState refresh pipeline that lands
# with B.1 full-wiring in a future session. They are STRICTLY
# sim-mode-only and 404 in live mode (we return 404 not 403 so the
# endpoint's existence is not advertised to non-sim deployments).
#
# Security model:
#   - 404 in live mode (mode != "sim"): handled by _verify_debug_mode()
#   - Bearer auth required: same as /dashboard/snapshot via verify_bearer
#   - bind_host=127.0.0.1 enforced by config/settings.py:HttpApiConfig
#     defaults + .env (BOT_HTTP_API_BIND_HOST=127.0.0.1). The aiohttp
#     server is bound loopback-only; non-loopback access is impossible
#     before this handler is even reached.
#   - Every injection logs at INFO with payload (audit trail).
# ---------------------------------------------------------------------------


class InjectPositionRequest(BaseModel):
    """Request body for POST /debug/inject-position.

    Strategy must be wire-form (``strategy1`` / ``strategy2`` /
    ``strategy3``); the helper does not translate from internal names.
    ``opened_at`` defaults to ``datetime.now(timezone.utc)`` if omitted.
    """

    market_id: str = Field(min_length=1)
    outcome: Any = Field(...)  # validated below — Literal["YES","NO"]
    quantity: float = Field(gt=0)
    avg_price: float = Field(gt=0, le=1.0)
    strategy: StrategyId  # wire-form: strategy1 / strategy2 / strategy3
    opened_at: Optional[str] = None  # ISO 8601 UTC; defaults to now()

    @field_validator("outcome")
    @classmethod
    def _validate_outcome(cls, v: Any) -> str:
        if v not in ("YES", "NO"):
            raise ValueError("outcome must be 'YES' or 'NO'")
        return v


def _verify_debug_mode(config: Any) -> bool:
    """Return True iff bot is in sim mode. Used to 404 the debug
    endpoints in live deployments.

    v0.2.9 3h-fix: the field lives at ``config.runtime.mode`` —
    ``RuntimeConfig.mode`` (config/settings.py:224), nested on
    ``BotConfig`` at ``config/settings.py:282``. NOT ``config.mode``.
    Reads via a getattr chain so a partially-initialised config (or a
    MagicMock without a fully-specified ``runtime`` attribute) returns
    False — the conservative default in any ambiguous case is "not
    sim" → 404, which preserves the security property even under
    config-shape drift.
    """
    runtime = getattr(config, "runtime", None)
    if runtime is None:
        return False
    mode = getattr(runtime, "mode", None)
    return mode == "sim"


def _parse_opened_at(raw: Optional[str]) -> datetime:
    """Parse an optional ISO 8601 UTC string into a tz-aware datetime.

    Accepts the bot's own canonical format (``"2026-05-21T13:00:00Z"``)
    plus the equivalent ``"+00:00"`` form. Defaults to ``utcnow`` when
    raw is None. Raises ``ValueError`` on unparseable input.
    """
    if raw is None:
        return datetime.now(timezone.utc)
    # Normalize "Z" suffix to "+00:00" for fromisoformat.
    normalized = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        # Treat naive datetimes as UTC for consistency.
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


async def inject_position_handler(
    request: web.Request, state: BotState, config: Any
) -> web.Response:
    """POST /debug/inject-position — sim-mode-only debug helper.

    Writes a synthetic Position into BotState.open_positions AND appends
    a corresponding FillRecord to BotState.recent_fills. Both writes
    happen under state.write_lock so the snapshot reads consistently.
    """
    # Security gate 1: 404 in non-sim mode. Check BEFORE auth so live
    # deployments don't even acknowledge the endpoint's existence
    # (404 response is identical whether the route exists or not).
    if not _verify_debug_mode(config):
        return web.Response(status=404, text="Not Found")

    # Security gate 2: Bearer auth.
    if not verify_bearer(request.headers, config):
        return web.Response(status=401, text="Unauthorized")

    try:
        body = await request.json()
    except Exception:
        return web.Response(status=400, text="Invalid JSON")

    try:
        req = InjectPositionRequest.model_validate(body)
    except ValidationError as exc:
        return web.Response(status=400, text=str(exc))

    try:
        opened_at = _parse_opened_at(req.opened_at)
    except (ValueError, TypeError) as exc:
        return web.Response(
            status=400,
            text=f"opened_at must be ISO 8601 UTC: {exc}",
        )

    position = Position(
        market_id=req.market_id,
        outcome=req.outcome,
        quantity=req.quantity,
        avg_price=req.avg_price,
        strategy=req.strategy,
        opened_at=opened_at,
    )

    key = f"{req.market_id}:{req.outcome}"
    action: str = "UP" if req.outcome == "YES" else "DOWN"
    fill = FillRecord(
        id=uuid.uuid4().hex,
        time=opened_at.strftime("%H:%M:%S"),
        symbol=req.market_id[:12],
        action=action,  # type: ignore[arg-type]
        price=req.avg_price,
        size=req.quantity,
        pnl=None,
        status="filled",
        strategy=req.strategy,
    )

    async with state.write_lock:
        state.open_positions[key] = position
        state.recent_fills.append(fill)

    logger.info(
        "debug_position_injected",
        market_id=req.market_id,
        outcome=req.outcome,
        quantity=req.quantity,
        avg_price=req.avg_price,
        strategy=req.strategy,
        opened_at=opened_at.isoformat().replace("+00:00", "Z"),
        fill_id=fill.id,
    )

    return web.json_response({"id": key, "status": "injected"})


async def clear_positions_handler(
    request: web.Request, state: BotState, config: Any
) -> web.Response:
    """POST /debug/clear-positions — sim-mode-only companion to
    inject-position. Resets BotState.open_positions and
    BotState.recent_fills so multiple inject smokes can run without a
    bot restart. Same security gates as inject."""
    if not _verify_debug_mode(config):
        return web.Response(status=404, text="Not Found")
    if not verify_bearer(request.headers, config):
        return web.Response(status=401, text="Unauthorized")

    async with state.write_lock:
        cleared_positions = len(state.open_positions)
        cleared_fills = len(state.recent_fills)
        state.open_positions.clear()
        state.recent_fills.clear()

    logger.info(
        "debug_positions_cleared",
        cleared_positions=cleared_positions,
        cleared_fills=cleared_fills,
    )
    return web.json_response(
        {
            "status": "cleared",
            "cleared_positions": cleared_positions,
            "cleared_fills": cleared_fills,
        }
    )


# ---------------------------------------------------------------------------
# /debug/inject-alert (v0.2.9 b9-fix; sim-mode only)
# ---------------------------------------------------------------------------
#
# Runtime-smoke companion to /debug/inject-position. The B.9 commit
# (v029-b9-alerts-wiring) wired ``utils.alerts.send_alert`` to populate
# both ``BotState.recent_alerts`` AND the WS broadcast — but no
# startup-time bot path actually FIRES send_alert under normal
# operation (startup warnings go through structlog, not send_alert).
# This endpoint fires send_alert on demand so smoke can curl an alert
# through the full production path (state append + WS broadcast) and
# eyeball the snapshot result.
#
# Architecture decision: call ``send_alert`` (rather than write
# directly to ``state.recent_alerts``). Reasons:
#   - Exercises the EXACT production code path. A direct write would
#     duplicate the AlertItem construction and risk silent drift from
#     the send_alert path.
#   - Lights up the WS broadcast too — browser devtools' WS-Messages
#     pane shows the frame, completing the runtime smoke.
#   - Endpoint stays a thin shim over the wired-up B.9 surface;
#     forward changes to send_alert flow through without endpoint
#     edits.
#
# Security gates identical to /debug/inject-position (3h precedent):
#   1. Sim-mode 404 BEFORE auth — endpoint absent on live deployments.
#   2. Bearer auth required.
#   3. State-side append happens under state.write_lock (inside
#      send_alert).
#   4. INFO log on successful injection (audit trail).
#   5. Pydantic InjectAlertPayload validation rejects malformed input
#      with 400 (not 422 — matches inject-position convention).
# ---------------------------------------------------------------------------


class InjectAlertPayload(BaseModel):
    """Request body for POST /debug/inject-alert.

    Three required fields mirroring ``utils.alerts.send_alert``'s public
    signature. ``id`` and ``time`` are computed inside send_alert; the
    caller does NOT supply them — they're filled with ``uuid4().hex``
    and ``strftime("%H:%M:%S")`` respectively at the broadcast site."""

    message: str = Field(min_length=1)
    level: Literal["info", "warning", "critical"] = "info"
    source: str = Field(default="debug-inject", min_length=1)


async def inject_alert_handler(
    request: web.Request, state: BotState, config: Any
) -> web.Response:
    """POST /debug/inject-alert — sim-mode-only debug helper.

    Fires ``utils.alerts.send_alert`` with the request body's message /
    level / source. The B.9 wiring then:
      1. Builds a wire-shape ``AlertItem`` Pydantic instance.
      2. Appends it to ``BotState.recent_alerts`` under write_lock.
      3. Broadcasts ``{"type":"ALERT", "payload": <AlertItem>}`` to WS
         subscribers (silently no-ops if no Emitter is registered).

    The endpoint then reads ``state.recent_alerts[-1]`` to return the
    AlertItem it just produced — letting curl-callers verify the
    round-trip without a second snapshot request.
    """
    # Security gate 1: 404 in non-sim mode (BEFORE auth — see 3h
    # precedent). _verify_debug_mode walks config.runtime.mode.
    if not _verify_debug_mode(config):
        return web.Response(status=404, text="Not Found")

    # Security gate 2: Bearer auth.
    if not verify_bearer(request.headers, config):
        return web.Response(status=401, text="Unauthorized")

    try:
        body = await request.json()
    except Exception:
        return web.Response(status=400, text="Invalid JSON")

    try:
        req = InjectAlertPayload.model_validate(body)
    except ValidationError as exc:
        return web.Response(status=400, text=str(exc))

    # Capture the deque length BEFORE send_alert so we can resolve the
    # appended item by index in a way that's robust against concurrent
    # injections (the endpoint holds no extra lock around send_alert;
    # send_alert itself acquires state.write_lock for the append).
    pre_len = len(state.recent_alerts)

    # Import locally to avoid a top-of-file circular concern; api/state
    # is already imported by this module so the utils/alerts → api/state
    # chain that send_alert relies on is already established.
    from utils.alerts import send_alert as _send_alert

    await _send_alert(req.message, level=req.level, source=req.source)

    # Read back the freshly appended AlertItem. send_alert's state-write
    # path is the only writer of recent_alerts; under sim-mode debug
    # smoke there are no concurrent appenders, so the new entry is the
    # last in the deque.
    if len(state.recent_alerts) <= pre_len:
        # send_alert silently no-op'd (state singleton not registered
        # via set_state). This shouldn't happen in production wiring
        # but surface it explicitly rather than returning a misleading
        # 200 with stale data.
        return web.Response(
            status=500,
            text=(
                "send_alert did not append to BotState.recent_alerts — "
                "is api.state.set_state wired at startup?"
            ),
        )
    alert = state.recent_alerts[-1]

    logger.info(
        "debug_alert_injected",
        message=req.message,
        level=req.level,
        source=req.source,
        alert_id=alert.id,
    )

    return web.json_response(alert.model_dump(mode="json"))


# ---------------------------------------------------------------------------
# /debug/inject-log (v0.2.9 B.8; sim-mode only)
# ---------------------------------------------------------------------------
#
# Runtime-smoke companion to /debug/inject-alert. Fires
# ``utils.logs.log_to_dashboard`` on demand so the full B.8 production
# path (state.recent_logs append, with 3f Option C lossy level map
# applied inside) is exercisable via curl. Snapshot reads recent_logs
# directly, so a subsequent GET /dashboard/snapshot shows the entry.
#
# NO WS broadcast — Zod WsMessageSchema has no LOG variant today
# (components/dashboard/schemas.ts:226); the snapshot is the only
# transport. If LOG WS frames are added later, the endpoint inherits
# the broadcast automatically (log_to_dashboard adds the path).
#
# Security gates identical to /debug/inject-alert + /debug/inject-
# position (3h + b9-fix precedent):
#   1. Sim-mode 404 BEFORE auth (endpoint absent on live deployments)
#   2. Bearer auth required
#   3. State-side append happens under state.write_lock (inside
#      log_to_dashboard)
#   4. INFO log on successful injection (audit trail)
#   5. Pydantic InjectLogPayload validation rejects malformed input
#      with 400 (NOT 422 — matches inject-position/alert convention)
# ---------------------------------------------------------------------------


class InjectLogPayload(BaseModel):
    """Request body for POST /debug/inject-log.

    Three fields mirroring ``utils.logs.log_to_dashboard``'s public
    signature. ``id`` and ``time`` are computed inside log_to_dashboard;
    the caller does NOT supply them.

    ``level`` accepts the INTERNAL 5-tier vocabulary (debug/info/warning/
    error/critical) — NOT the wire-side 4-tier enum (INFO/WARN/ERROR/
    EXEC). log_to_dashboard applies the 3f Option C lossy map at the
    boundary (debug → DROP, critical → ERROR, etc.). Documents this so
    curl-callers don't accidentally pass wire-form uppercase.
    """

    message: str = Field(min_length=1)
    level: Literal["debug", "info", "warning", "error", "critical"] = "info"
    source: str = Field(default="debug-inject", min_length=1)


async def inject_log_handler(
    request: web.Request, state: BotState, config: Any
) -> web.Response:
    """POST /debug/inject-log — sim-mode-only debug helper.

    Fires ``utils.logs.log_to_dashboard``. The B.8 wiring applies the
    3f Option C lossy level map, builds a wire-shape BotLogEntry, and
    appends to BotState.recent_logs under write_lock.

    Special-case response: when the caller passes ``level="debug"``,
    log_to_dashboard intentionally DROPS the entry (debug tier is
    below the wire noise floor). The endpoint returns 204 No Content
    in that case so callers can observe the drop without thinking the
    request errored. All other levels → 200 with the BotLogEntry body.
    """
    # Security gate 1: 404 in non-sim mode (BEFORE auth — see 3h
    # precedent). _verify_debug_mode walks config.runtime.mode.
    if not _verify_debug_mode(config):
        return web.Response(status=404, text="Not Found")

    # Security gate 2: Bearer auth.
    if not verify_bearer(request.headers, config):
        return web.Response(status=401, text="Unauthorized")

    try:
        body = await request.json()
    except Exception:
        return web.Response(status=400, text="Invalid JSON")

    try:
        req = InjectLogPayload.model_validate(body)
    except ValidationError as exc:
        return web.Response(status=400, text=str(exc))

    pre_len = len(state.recent_logs)

    from utils.logs import log_to_dashboard as _log_to_dashboard

    await _log_to_dashboard(req.message, level=req.level, source=req.source)

    if len(state.recent_logs) <= pre_len:
        # log_to_dashboard intentionally dropped the entry — either
        # the debug-tier drop (3f Option C) or no BotState was
        # registered. The latter shouldn't happen in production
        # wiring; the former is expected behavior for level="debug".
        if req.level == "debug":
            # Explicit 204: caller asked for a debug log, dashboard
            # doesn't surface debug. Return No Content so the caller
            # can verify the drop.
            logger.info(
                "debug_log_dropped_per_policy",
                level=req.level,
                source=req.source,
            )
            return web.Response(status=204)
        # Anything else: log_to_dashboard no-op'd unexpectedly (state
        # singleton not registered). Surface explicitly rather than
        # returning stale data.
        return web.Response(
            status=500,
            text=(
                "log_to_dashboard did not append to BotState.recent_logs — "
                "is api.state.set_state wired at startup?"
            ),
        )

    entry = state.recent_logs[-1]

    logger.info(
        "debug_log_injected",
        message=req.message,
        level=req.level,
        source=req.source,
        log_id=entry.id,
        wire_level=entry.level,
    )

    return web.json_response(entry.model_dump(mode="json"))


# ---------------------------------------------------------------------------
# Closure factory + route registration
# ---------------------------------------------------------------------------


def _make_handler(fn: Any, state: BotState, config: Any) -> Any:
    async def _handler(request: web.Request) -> web.Response:
        return await fn(request, state, config)
    return _handler


def setup_routes(app: web.Application, state: BotState, config: Any) -> None:
    """Register the three D.2.2 REST routes onto an aiohttp Application.

    v0.2.9 3h adds two sim-mode-only debug routes
    (``/debug/inject-position`` and ``/debug/clear-positions``); their
    handlers self-gate via ``_verify_debug_mode`` so registration is
    unconditional (registration vs. 404-from-handler keeps the routing
    table identical between sim and live deployments — same surface,
    different runtime answer)."""
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
    # v0.2.9 3h debug-injection endpoints. 404-gated in live mode by
    # the handlers themselves (see _verify_debug_mode).
    app.router.add_post(
        "/debug/inject-position",
        _make_handler(inject_position_handler, state, config),
    )
    app.router.add_post(
        "/debug/clear-positions",
        _make_handler(clear_positions_handler, state, config),
    )
    # v0.2.9 b9-fix: alerts runtime-smoke companion. Same 404-gate +
    # Bearer-auth surface as inject-position; uses send_alert internally
    # so production B.9 wiring is exercised end-to-end.
    app.router.add_post(
        "/debug/inject-alert",
        _make_handler(inject_alert_handler, state, config),
    )
    # v0.2.9 B.8: logs runtime-smoke companion. Same 404-gate +
    # Bearer-auth surface; uses log_to_dashboard internally so the
    # 3f Option C lossy level map is applied. Returns 204 No Content
    # when level="debug" (intentional drop per policy); 200 + body
    # otherwise.
    app.router.add_post(
        "/debug/inject-log",
        _make_handler(inject_log_handler, state, config),
    )
