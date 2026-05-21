"""
api/schemas.py
Pydantic v2 models that mirror the dashboard's Zod schemas from
docs/BOT_CONTRACT.md Appendix B.  These are the canonical data-transfer
shapes used for JSON serialisation in REST responses and WS messages.

OQ-4 strategy ID mapping (D.2.1 §OQ-4):
  contract "strategy1" ↔ internal "intra_arbitrage"
  contract "strategy2" ↔ internal "momentum_latency"
  contract "strategy3" ↔ internal "market_maker"
  "smart_money_copy" has no contract ID in v1.0 and is not exposed.
"""
from __future__ import annotations

import re
from typing import Annotated, Any, Literal, Optional, Union

from pydantic import BaseModel, Field, field_validator

# ---------------------------------------------------------------------------
# OQ-4 — stable strategy-ID mapping constants
# ---------------------------------------------------------------------------

STRATEGY_ID_TO_INTERNAL: dict[str, str] = {
    "strategy1": "intra_arbitrage",
    "strategy2": "momentum_latency",
    "strategy3": "market_maker",
}

INTERNAL_TO_STRATEGY_ID: dict[str, str] = {
    v: k for k, v in STRATEGY_ID_TO_INTERNAL.items()
}

# Stable contract type — used everywhere a strategy ID is needed.
StrategyId = Literal["strategy1", "strategy2", "strategy3"]


def bot_to_dashboard(internal_name: str) -> str:
    """Translate an internal strategy name to the dashboard contract id.

    Raises ValueError if `internal_name` has no mapping (e.g. smart_money_copy,
    which has no contract id in v1.0). Defensive — main.py's strategy gate
    should prevent unmapped strategies from reaching the wire today, but the
    helper must refuse rather than silently corrupt the wire shape.
    """
    try:
        return INTERNAL_TO_STRATEGY_ID[internal_name]
    except KeyError as exc:
        raise ValueError(
            f"No dashboard contract id for internal strategy {internal_name!r}; "
            f"v1.0 contract supports only {sorted(INTERNAL_TO_STRATEGY_ID.keys())!r}"
        ) from exc


def dashboard_to_bot(contract_id: str) -> str:
    """Translate a dashboard contract id to the internal strategy name.

    Raises ValueError if `contract_id` is not one of strategy1/strategy2/strategy3.
    """
    try:
        return STRATEGY_ID_TO_INTERNAL[contract_id]
    except KeyError as exc:
        raise ValueError(
            f"No internal strategy for contract id {contract_id!r}; "
            f"v1.0 contract supports only {sorted(STRATEGY_ID_TO_INTERNAL.keys())!r}"
        ) from exc


# ---------------------------------------------------------------------------
# GlobalStats
# ---------------------------------------------------------------------------


class GlobalStats(BaseModel):
    """Mirrors dashboard GlobalStatsSchema in components/dashboard/schemas.ts.
    Per docs/BOT_CONTRACT.md v1.0, all 8 fields are required. lastUpdatedAt
    is epoch ms; the dashboard's stale-snapshot gate at
    lib/refreshDashboard.ts reads globalStats.lastUpdatedAt."""

    portfolioValue: float
    pnlToday: float
    pnlMonth: float
    pnlAllTime: float
    winRate: float
    totalTrades: int
    streak: int
    lastUpdatedAt: int  # epoch ms — moved here from DashboardSnapshot top-level per A.2


# ---------------------------------------------------------------------------
# StrategyCardData
# ---------------------------------------------------------------------------


class StrategyCardData(BaseModel):
    """Mirrors dashboard StrategyCardDataSchema.

    Reshaped in D.2.4 step 3 (STRATEGY_UPDATE) to match dashboard
    components/dashboard/schemas.ts:44. Eight fields are nullable
    because the bot does not currently track per-strategy telemetry
    (OQ-3 stub state; see BotState in api/state.py). sparkline defaults
    to [] rather than null because empty time-series is the natural
    representation of "no data points yet". See
    docs/audit/d24-emit-site-audit.md §10 (STRATEGY_UPDATE drift +
    Level-1 escalation).
    """

    id: StrategyId
    name: str
    status: Literal["live", "paused"]
    pnlToday: Optional[float] = None
    pnlMonth: Optional[float] = None
    winRate: Optional[float] = None
    tradesToday: Optional[int] = None
    avgHoldMinutes: Optional[float] = None
    lastSignal: Optional[Literal["LONG", "SHORT", "EXIT"]] = None
    # OQ-7 sequence note: sequenceNumber deferred to v1.1+
    pipelineStep: int  # 1=intra_arbitrage, 2=momentum_latency, 3=market_maker
    exposure: Optional[float] = None
    sparkline: list[float] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# OpenPosition
# ---------------------------------------------------------------------------


class OpenPosition(BaseModel):
    """Mirrors dashboard OpenPositionSchema — canonical Zod source at
    ``components/dashboard/schemas.ts:67`` + ``BOT_CONTRACT.md`` v1.0
    line 115. Eleven fields, all required, in wire order.

    v0.2.9 sub-phase 3d-fix corrects the 3d landing (f9d1314) which
    followed the now-deprecated audit row B.3 (different field names).
    Team A's A-2 reconciliation matrix established that BOT_CONTRACT +
    Zod are canonical and agree; the audit doc was the outlier.

    Wire fields (in Zod order):

    - ``id``                — collapsed identifier (see
      ``api/routes_rest.py:_collapse_position_id``). For Polymarket:
      ``f"{marketId}:{outcome}"``; for spot: ``f"{venue}:{symbol}"``.
    - ``tradeId``           — per-fill trade identifier. v0.2.9 stopgap
      (Decision B): bot ships ``tradeId = id`` because internal
      ``utils.types.Position`` has no per-fill id. TODO(B.3-wiring)
      when B.1 fill runtime lands.
    - ``symbol``            — display symbol (Polymarket: a truncated
      market_id; spot: BASE/QUOTE).
    - ``strategy``          — strategy identifier (free string, not
      ``StrategyId`` enum — dashboard treats this loosely).
    - ``direction``         — ``"Long"`` or ``"Short"`` (title case per
      ``BOT_CONTRACT.md`` line 472; lowercase ``long``/``short`` from
      the previous f9d1314 landing was wrong).
    - ``entryPrice``        — number, entry price.
    - ``currentPrice``      — number, mark price. TODO(B.3-wiring):
      currently stubbed to ``entryPrice`` so PnL math trivially
      resolves to 0.0; wire real mark when B.1 fill runtime lands.
    - ``size``              — number (renamed from ``quantity`` in 3d-fix).
    - ``unrealizedPnlPct``  — number, percent. TODO(B.3-wiring): 0.0
      until mark-pricing surfaces.
    - ``unrealizedPnlUsd``  — number, USD. TODO(B.3-wiring): 0.0 until
      mark-pricing surfaces.
    - ``timeOpen``          — ISO 8601 UTC string with ``Z`` suffix
      (e.g. ``"2026-05-21T13:00:00Z"``). Renamed from epoch-ms
      ``openedAt`` in 3d-fix.
    """

    id: str
    tradeId: str
    symbol: str
    strategy: str
    direction: Literal["Long", "Short"]
    entryPrice: float
    currentPrice: float
    size: float
    unrealizedPnlPct: float
    unrealizedPnlUsd: float
    timeOpen: str


# ---------------------------------------------------------------------------
# FillRecord
# ---------------------------------------------------------------------------


class FillRecord(BaseModel):
    """Mirrors dashboard FillSchema. See docs/audit/d24-emit-site-audit.md §10
    (FILL drift discovery + reconciliation). action is UP/DOWN (net market direction).

    Dropped fields from prior shape: ``marketId``, ``outcome: Literal["YES","NO"]``,
    ``fees``. Zero non-test consumers verified pre-batch.

    Renamed: ``quantity`` -> ``size`` (dashboard naming), ``ts: int`` (epoch ms)
    -> ``time: str`` ("HH:MM:SS" per dashboard handoff §6.3).

    Added: ``symbol`` (display-only mapping to ``market_id[:12]`` at emit time),
    ``status: Literal["filled","pending"]`` (hardcoded "filled" at the main.py
    emit site by guard-construction).

    ``pnl`` remains ``Optional[float]``: the bot does NOT know realized pnl at
    fill time. Dashboard ``FillSchema.pnl`` was updated to nullable in a paired
    dashboard commit. See d24-emit-site-audit.md §10 for the rationale on
    why this is the first place "dashboard canonical" did not hold."""

    id: str
    time: str  # "HH:MM:SS" per dashboard handoff §6.3
    symbol: str  # display-only mapping; see docstring
    action: Literal["UP", "DOWN"]
    price: float
    size: float
    pnl: Optional[float] = None
    status: Literal["filled", "pending"] = "filled"
    strategy: StrategyId


# ---------------------------------------------------------------------------
# RiskMetrics
# ---------------------------------------------------------------------------


class RiskMetrics(BaseModel):
    """Mirrors dashboard RiskMetricsSchema — canonical Zod source at
    ``components/dashboard/schemas.ts:96`` + ``BOT_CONTRACT.md`` v1.0
    line 119. Six fields, in wire order.

    v0.2.9 sub-phase 3e correction: the 5bc2b6b risk-stub commit
    shipped the v0.2.8 legacy field set (currentDrawdown, sharpeRatio,
    kellyFraction, pairCorrelation, dailyVaR), which never matched
    the contract. Audit row B.5 incorrectly described a 10-field
    shape; Team A's A-2 reconciliation matrix established the
    canonical 6-field set above (Zod + BOT_CONTRACT.md agree).

    Wire fields (Zod field order):

    - ``maxDrawdown``     — number | null. Observed peak-to-trough
      drawdown, percent. TODO(B.5-wiring): compute from equity_history
      pipeline when full wiring lands.
    - ``sharpe``          — number | null. Sharpe ratio. TODO(B.5-
      wiring): compute from returns sample when sufficient samples
      exist.
    - ``var95``           — number | null. 95% Value-at-Risk, USD.
      TODO(B.5-wiring): compute from returns distribution.
    - ``leverage``        — number, hardcoded ``1.0``. Polymarket
      binary spot is structurally non-leveraged — full notional paid
      up-front, no margin. Documentary constant; if a leveraged venue
      is ever added, this is the single source of truth to change.
    - ``exposurePct``     — number | null. Net exposure as percent of
      portfolio. TODO(B.5-wiring): needs per-position mark surface.
    - ``liquidationRisk`` — Literal["low","medium","high"] | null.
      Enum, NOT a number. TODO(B.5-wiring): derive from drawdown
      proximity to ``cfg.risk.daily_drawdown_limit_pct``.
    """

    maxDrawdown: Optional[float] = None
    sharpe: Optional[float] = None
    var95: Optional[float] = None
    leverage: float = 1.0
    exposurePct: Optional[float] = None
    liquidationRisk: Optional[Literal["low", "medium", "high"]] = None


# ---------------------------------------------------------------------------
# StrategyConfig  (full config shape returned by snapshot + GET endpoints)
# ---------------------------------------------------------------------------

# v0.2.9 (L2 E.2): per-element BASE/QUOTE pair regex matches the dashboard's
# STRATEGY_ACTIVE_PAIR_REGEX in components/dashboard/schemas.ts. Two to six
# uppercase letters per side, single forward slash separator.
_BASE_QUOTE_RE = re.compile(r"^[A-Z]{2,6}/[A-Z]{2,6}$")


def _validate_active_pairs_list(
    v: Optional[list[str]],
) -> Optional[list[str]]:
    """Per-element BASE/QUOTE validator shared by the three strategy
    config models. None is accepted; an empty list is accepted; any
    non-conforming element raises ValueError."""
    if v is None:
        return v
    for pair in v:
        if not _BASE_QUOTE_RE.match(pair):
            raise ValueError(
                "activePairs entries must match BASE/QUOTE (e.g. 'BTC/USDT'); "
                f"got {pair!r}"
            )
    return v


class StrategyConfig(BaseModel):
    """Mirrors dashboard StrategyConfigSchema. Returned as part of the
    snapshot. v0.2.9 (L2 E.2): riskPct is the contract form (0.1–10.0
    percent points), id → strategyId, activePairs is a list of
    BASE/QUOTE strings."""

    strategyId: StrategyId
    enabled: bool
    maxPositionSize: float = Field(ge=0.1, le=100.0)
    riskPct: float = Field(ge=0.1, le=10.0)
    activePairs: Optional[list[str]] = None

    @field_validator("activePairs")
    @classmethod
    def _validate_active_pairs(
        cls, v: Optional[list[str]]
    ) -> Optional[list[str]]:
        return _validate_active_pairs_list(v)


class StrategyConfigPatch(BaseModel):
    """Request body for POST /api/strategies/config. Mirrors
    strategyPatchBodySchema. v0.2.9 (L2 E.2) renames match StrategyConfig."""

    strategyId: StrategyId
    enabled: Optional[bool] = None
    maxPositionSize: Optional[float] = Field(default=None, ge=0.1, le=100.0)
    riskPct: Optional[float] = Field(default=None, ge=0.1, le=10.0)
    activePairs: Optional[list[str]] = None

    @field_validator("activePairs")
    @classmethod
    def _validate_active_pairs(
        cls, v: Optional[list[str]]
    ) -> Optional[list[str]]:
        return _validate_active_pairs_list(v)


# ---------------------------------------------------------------------------
# BotLogEntry
# ---------------------------------------------------------------------------


class BotLogEntry(BaseModel):
    """Mirrors BotLogEntrySchema."""

    ts: int  # epoch ms
    level: Literal["debug", "info", "warning", "error", "critical"]
    message: str
    context: Optional[dict[str, Any]] = None


# ---------------------------------------------------------------------------
# AlertItem
# ---------------------------------------------------------------------------


class AlertItem(BaseModel):
    """Mirrors dashboard AlertItemSchema. See docs/audit/d24-emit-site-audit.md §10."""

    id: str
    time: str  # "HH:MM:SS" per dashboard handoff §6.3
    severity: Literal["info", "warning", "critical"]
    source: str
    message: str


# ---------------------------------------------------------------------------
# EquityPoint
# ---------------------------------------------------------------------------


class EquityPoint(BaseModel):
    """Mirrors EquityPointSchema.  Used for the equity history sparkline."""

    ts: int  # epoch ms
    equity: float


# ---------------------------------------------------------------------------
# MonthlyPnl
# ---------------------------------------------------------------------------


class MonthlyPnl(BaseModel):
    """Mirrors MonthlyPnlSchema."""

    month: str  # ISO-8601 year-month, e.g. "2026-05"
    pnl: float
    trades: int


# ---------------------------------------------------------------------------
# BacktestPoint
# ---------------------------------------------------------------------------


class BacktestPoint(BaseModel):
    """Mirrors BacktestPointSchema."""

    ts: int  # epoch ms
    equity: float
    drawdown: float


# ---------------------------------------------------------------------------
# StrategyToggle  (lightweight body for enable/disable operations)
# ---------------------------------------------------------------------------


class StrategyToggle(BaseModel):
    """Mirrors StrategyToggleSchema."""

    id: StrategyId
    enabled: bool


# ---------------------------------------------------------------------------
# DashboardSnapshot  (top-level composite — all fields Optional per contract)
# ---------------------------------------------------------------------------


class DashboardSnapshot(BaseModel):
    """
    Full snapshot returned by GET /dashboard/snapshot.
    Every field is Optional so the serialiser can emit partial snapshots
    during startup or when sub-systems are unavailable.
    OQ-3 stub fields (risk_metrics, equity_history, monthly_pnl, backtest,
    recent_logs, recent_alerts) are present so D.2.3's serialiser can emit
    empty arrays without schema changes.
    """

    # Core portfolio
    globalStats: Optional[GlobalStats] = None

    # Per-strategy cards
    strategies: Optional[list[StrategyCardData]] = None

    # Positions and fills — v0.2.9 (audit row A.1) renames:
    #   openPositions → positions, recentFills → fills
    positions: Optional[list[OpenPosition]] = None
    fills: Optional[list[FillRecord]] = None

    # Strategy configs
    strategyConfigs: Optional[list[StrategyConfig]] = None

    # OQ-3 stub fields — served as empty lists until telemetry is wired (D.2.4+).
    # v0.2.9 (audit row A.1) renames: riskMetrics → risk, equityHistory → equity,
    # recentLogs → logs, recentAlerts → alerts. Inner payload shapes unchanged
    # (those land in sub-phases 3d–3g).
    risk: Optional[RiskMetrics] = None
    equity: Optional[list[EquityPoint]] = None              # OQ-3 stub
    monthlyPnl: Optional[list[MonthlyPnl]] = None           # OQ-3 stub
    backtest: Optional[list[BacktestPoint]] = None          # OQ-3 stub
    logs: Optional[list[BotLogEntry]] = None                # OQ-3 stub
    alerts: Optional[list[AlertItem]] = None                # OQ-3 stub

    # lastUpdatedAt now lives inside globalStats per audit row A.2 — see
    # GlobalStats.lastUpdatedAt and docs/BOT_CONTRACT.md v1.0.


# ---------------------------------------------------------------------------
# StopResponse
# ---------------------------------------------------------------------------


class StopResponse(BaseModel):
    """Response body for POST /emergency-stop."""

    ok: bool = True
    stoppedAt: Optional[int] = None  # epoch ms; None if clock unavailable


# ---------------------------------------------------------------------------
# StrategyRuntimeConfig  — internal mutable config held on BotState
# ---------------------------------------------------------------------------


class StrategyRuntimeConfig(BaseModel):
    """
    Mutable per-strategy config stored in BotState.strategy_configs.
    Named per spec (D.2.1 §OQ-5). enabled defaults True so the bot runs
    all three strategies on startup unless explicitly disabled.

    v0.2.9 (L2 E.2): renames match the wire StrategyConfig. The default
    riskPct=2.5 (2.5% per trade) replaces the prior riskBudget=0.33 —
    the 0.33 fraction was outside the contract's 0.1–10.0 percent range
    and was an unintentionally aggressive 33% sizing default.
    """

    strategyId: StrategyId
    enabled: bool = True
    maxPositionSize: float = Field(default=100.0, ge=0.1, le=100.0)
    riskPct: float = Field(default=2.5, ge=0.1, le=10.0)
    activePairs: Optional[list[str]] = None

    @field_validator("activePairs")
    @classmethod
    def _validate_active_pairs(
        cls, v: Optional[list[str]]
    ) -> Optional[list[str]]:
        return _validate_active_pairs_list(v)

    def apply_patch(self, patch: StrategyConfigPatch) -> "StrategyRuntimeConfig":
        """Return a new StrategyRuntimeConfig with patch fields applied.

        strategyId is excluded from the patch payload — it identifies the
        target config, not a mutation."""
        data = self.model_dump()
        patch_data = patch.model_dump(exclude_none=True, exclude={"strategyId"})
        data.update(patch_data)
        return StrategyRuntimeConfig(**data)

    def to_contract(self) -> StrategyConfig:
        """Convert to the contract-facing StrategyConfig shape."""
        return StrategyConfig(
            strategyId=self.strategyId,
            enabled=self.enabled,
            maxPositionSize=self.maxPositionSize,
            riskPct=self.riskPct,
            activePairs=self.activePairs,
        )


# ---------------------------------------------------------------------------
# PriceTick  (internal helper used by emit path; not a contract shape)
# ---------------------------------------------------------------------------


class PriceTick(BaseModel):
    """Lightweight price tick used internally by the TICK WS message."""

    marketId: str
    price: float
    ts: Optional[float] = None  # Unix epoch seconds; None if not available


# ---------------------------------------------------------------------------
# WebSocket message union
# ---------------------------------------------------------------------------


# Payload wrapper classes for TICK/FILL/ALERT/STRATEGY_UPDATE were
# flattened in D.2.4 (steps 1-3) to match the dashboard's WsMessageSchema
# discriminated union in components/dashboard/schemas.ts:215. See
# d24-emit-site-audit.md §10.


class StrategyUpdateFields(BaseModel):
    """Partial of StrategyCardData per BOT_CONTRACT.md:413, 609.

    Allowed keys (all Optional, omit those not changing): name, status,
    pnlToday, pnlMonth, winRate, tradesToday, avgHoldMinutes, lastSignal,
    pipelineStep, exposure, sparkline. Empty {} is valid per contract.

    Defined before StrategyUpdate so StrategyUpdate.updates can reference
    it without a forward-ref (pydantic v2 forward-refs require
    model_rebuild() and that's an avoidable extra step).

    HISTORY (v0.2.8 post-tag correction): an earlier version of this
    class held the 4 StrategyConfigPatch fields (enabled,
    maxPositionSize, riskBudget, activePairs) — see d1d2276. That
    shape was wrong: STRATEGY_UPDATE per the contract carries
    card-telemetry deltas, not config-patch deltas. The dashboard's
    zod schema (StrategyCardUpdatesSchema = StrategyCardDataSchema
    .omit({id: true}).partial()) stripped the old shape entirely.
    See d24-emit-site-audit.md §6.0 (three strategy surfaces) and
    §10 (correction block).

    NO EMIT SITE WIRED IN v0.2.8: this class is contract-correct but
    nothing currently constructs it. utils/strategy_updates.py has the
    broadcast infrastructure ready; the caller (the strategy-telemetry
    pipeline) lands in v0.2.9.
    """

    name: Optional[str] = None
    status: Optional[Literal["live", "paused"]] = None
    pnlToday: Optional[float] = None
    pnlMonth: Optional[float] = None
    winRate: Optional[float] = None
    tradesToday: Optional[int] = None
    avgHoldMinutes: Optional[float] = None
    lastSignal: Optional[Literal["LONG", "SHORT", "EXIT"]] = None
    pipelineStep: Optional[int] = None
    exposure: Optional[float] = None
    sparkline: Optional[list[float]] = None


class StrategyUpdate(BaseModel):
    """STRATEGY_UPDATE payload per BOT_CONTRACT.md:413.

    Envelope: {strategyId, updates: partial-of-StrategyCardData}.
    Matches the dashboard's StrategyUpdateSchema exactly — both sides
    use the same 11-field card-telemetry partial. No subset/superset
    asymmetry.

    HISTORY (v0.2.8 post-tag correction): an earlier version of this
    docstring claimed the bot was "intentionally narrower than dashboard
    StrategyCardUpdatesSchema" because the bot emitted the 4
    StrategyRuntimeConfig fields. That was a misread of the contract —
    STRATEGY_UPDATE carries card-telemetry deltas, not config-patch
    deltas — and the resulting wire-up was a silent no-op on the
    dashboard side (zod's .partial() stripped all 4 unknown keys).
    See d24-emit-site-audit.md §6.0 (three strategy surfaces) and
    §10 (correction block).
    """

    strategyId: StrategyId
    updates: StrategyUpdateFields


class WsTick(BaseModel):
    type: Literal["TICK"] = "TICK"
    payload: PriceTick


class WsFill(BaseModel):
    type: Literal["FILL"] = "FILL"
    payload: FillRecord


class WsAlert(BaseModel):
    type: Literal["ALERT"] = "ALERT"
    payload: AlertItem


class WsStrategyUpdate(BaseModel):
    type: Literal["STRATEGY_UPDATE"] = "STRATEGY_UPDATE"
    payload: StrategyUpdate


class WsPong(BaseModel):
    type: Literal["PONG"] = "PONG"


# Discriminated union of all outbound WS message types.
WsMessage = Annotated[
    Union[WsTick, WsFill, WsAlert, WsStrategyUpdate, WsPong],
    Field(discriminator="type"),
]
