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
from typing import Any, Optional

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
from utils.types import Position

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Wire-shape helpers
# ---------------------------------------------------------------------------


# v0.2.9 3f (Option C, Jose 2026-05-21): internal→wire log-level map.
# Python's standard 5-tier logging set (debug/info/warning/error/critical)
# collapses lossy to the 4-tier dashboard enum (INFO/WARN/ERROR/EXEC).
# `debug` is dropped entirely; `critical` collapses to ERROR (mirrors
# the E.3 alert-severity precedent). EXEC stays in the wire enum as a
# vestigial slot — bot NEVER produces it. See BotLogEntry docstring in
# api/schemas.py.
_LOG_LEVEL_MAP: dict[str, Optional[str]] = {
    "debug": None,        # drop debug entries from the wire
    "info": "INFO",
    "warning": "WARN",
    "error": "ERROR",
    "critical": "ERROR",  # lossy collapse to highest wire tier
}


def _map_log_level(internal: str) -> Optional[str]:
    """Map a Python-logging level (lowercase) to the wire BotLogEntry
    ``level`` enum literal, or ``None`` to drop the entry.

    Raises ``ValueError`` on unknown levels — silent corruption of the
    wire enum would be harder to debug than a surfaced exception at the
    boundary. Mirrors the defensive policy used by
    ``_collapse_position_id``.
    """
    try:
        return _LOG_LEVEL_MAP[internal]
    except KeyError as exc:
        raise ValueError(
            f"Unknown internal log level {internal!r}; "
            f"expected one of {sorted(_LOG_LEVEL_MAP.keys())!r}"
        ) from exc


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
        # v0.2.9 3e: emit a zero-arg RiskMetrics() — canonical 6-field
        # shape per components/dashboard/schemas.ts:96 + BOT_CONTRACT.md
        # v1.0 line 119. Five fields default to None (Zod nullable);
        # leverage defaults to 1.0 (spot-venue structural constant —
        # see RiskMetrics docstring). Real values for the five nullable
        # fields land with B.5 full wiring (equity-history + mark-
        # pricing pipelines).
        risk=RiskMetrics(),
        # equity: stays as `[]` OQ-3 stub. v0.2.9 3f established the
        # canonical 3-field EquityPoint shape (day, equity, pnl). When
        # the equity-history pipeline lands (B.7-wiring), build each
        # point inline here as:
        #   EquityPoint(day=pt.date.isoformat(),  # "YYYY-MM-DD"
        #               equity=pt.equity,
        #               pnl=pt.pnl)
        # No internal source today — BotState.equity_history is also a
        # stub `[]`. See EquityPoint docstring in api/schemas.py.
        equity=[],          # OQ-3 stub — TODO(B.7-wiring)
        monthlyPnl=[],      # OQ-3 stub
        backtest=[],        # OQ-3 stub
        # logs: stays as `[]` OQ-3 stub. v0.2.9 3f established the
        # canonical 4-field BotLogEntry shape (id, time, level, message)
        # with the 5→4 tier lossy map locked as Option C. When the log-
        # tap pipeline lands (B.8-wiring), build each entry inline as:
        #   wire_level = _map_log_level(internal_entry.level.lower())
        #   if wire_level is None:   # debug — drop
        #       continue
        #   BotLogEntry(id=internal_entry.id,
        #               time=internal_entry.timestamp.strftime("%H:%M:%S"),
        #               level=wire_level,
        #               message=internal_entry.message)
        # Bot NEVER emits "EXEC"; that vestigial slot is for future
        # B.1-fill-runtime reconsideration.
        logs=[],            # OQ-3 stub — TODO(B.8-wiring)
        # alerts: stays as `[]` OQ-3 stub. AlertItem schema was already
        # canonical pre-3f; only the severity-collapse helper changed.
        # When the alert pipeline lands (B.9-wiring), build each entry as:
        #   wire_sev = _map_alert_severity(internal_alert.severity.lower())
        #   if wire_sev is None:   # debug — drop
        #       continue
        #   AlertItem(id=..., time=..., severity=wire_sev,
        #             source=..., message=...)
        # E.3 collapse: internal error→critical at the boundary.
        alerts=[],          # OQ-3 stub — TODO(B.9-wiring)
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
