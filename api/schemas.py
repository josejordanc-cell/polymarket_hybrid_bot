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

# ---------------------------------------------------------------------------
# GlobalStats
# ---------------------------------------------------------------------------


class GlobalStats(BaseModel):
    """Mirrors GlobalStatsSchema in the dashboard's Zod schemas."""

    totalEquity: float
    dayPnl: float
    dayPnlPct: float
    openPositions: int
    totalTrades: int
    winRate: float
    sharpeRatio: Optional[float] = None
    maxDrawdown: Optional[float] = None


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
    """Mirrors OpenPositionSchema."""

    id: str
    marketId: str
    outcome: Literal["YES", "NO"]
    direction: Literal["Long", "Short"]
    quantity: float
    avgPrice: float
    currentPrice: Optional[float] = None
    unrealisedPnl: Optional[float] = None
    strategy: StrategyId
    openedAt: int  # epoch ms


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
    """Mirrors RiskMetricsSchema."""

    maxDrawdown: float
    currentDrawdown: float
    sharpeRatio: Optional[float] = None
    kellyFraction: Optional[float] = None
    pairCorrelation: Optional[float] = None
    dailyVaR: Optional[float] = None


# ---------------------------------------------------------------------------
# StrategyConfig  (full config shape returned by snapshot + GET endpoints)
# ---------------------------------------------------------------------------

_ACTIVE_PAIRS_RE = re.compile(r"^[A-Z0-9/-]+(,[A-Z0-9/-]+)*$")


class StrategyConfig(BaseModel):
    """Mirrors StrategyConfigSchema.  Returned as part of the snapshot."""

    id: StrategyId
    enabled: bool
    maxPositionSize: float = Field(gt=0)
    riskBudget: float = Field(ge=0.0, le=1.0)
    activePairs: Optional[str] = None  # comma-separated market-id list

    @field_validator("activePairs")
    @classmethod
    def _validate_active_pairs(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v == "":
            return v
        if not _ACTIVE_PAIRS_RE.match(v):
            raise ValueError(
                "activePairs must be a comma-separated list of "
                "uppercase alphanumeric market identifiers, e.g. 'BTC-USD,ETH-USD'"
            )
        return v


class StrategyConfigPatch(BaseModel):
    """Request body for POST /api/strategies/config.  Mirrors strategyPatchBodySchema."""

    id: StrategyId
    enabled: Optional[bool] = None
    maxPositionSize: Optional[float] = Field(default=None, gt=0)
    riskBudget: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    activePairs: Optional[str] = None

    @field_validator("activePairs")
    @classmethod
    def _validate_active_pairs(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v == "":
            return v
        if not _ACTIVE_PAIRS_RE.match(v):
            raise ValueError(
                "activePairs must be a comma-separated list of "
                "uppercase alphanumeric market identifiers"
            )
        return v


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

    # Positions and fills
    openPositions: Optional[list[OpenPosition]] = None
    recentFills: Optional[list[FillRecord]] = None

    # Strategy configs
    strategyConfigs: Optional[list[StrategyConfig]] = None

    # OQ-3 stub fields — served as empty lists until telemetry is wired (D.2.4+)
    riskMetrics: Optional[RiskMetrics] = None
    equityHistory: Optional[list[EquityPoint]] = None       # OQ-3 stub
    monthlyPnl: Optional[list[MonthlyPnl]] = None          # OQ-3 stub
    backtest: Optional[list[BacktestPoint]] = None          # OQ-3 stub
    recentLogs: Optional[list[BotLogEntry]] = None          # OQ-3 stub
    recentAlerts: Optional[list[AlertItem]] = None          # OQ-3 stub

    # Monotonic snapshot counter (see D.2.1 §4 last_updated_at_ms)
    lastUpdatedAt: Optional[int] = None  # epoch ms


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
    """

    id: StrategyId
    enabled: bool = True
    maxPositionSize: float = Field(default=100.0, gt=0)
    riskBudget: float = Field(default=0.33, ge=0.0, le=1.0)
    activePairs: Optional[str] = None

    @field_validator("activePairs")
    @classmethod
    def _validate_active_pairs(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v == "":
            return v
        if not _ACTIVE_PAIRS_RE.match(v):
            raise ValueError(
                "activePairs must be a comma-separated list of "
                "uppercase alphanumeric market identifiers"
            )
        return v

    def apply_patch(self, patch: StrategyConfigPatch) -> "StrategyRuntimeConfig":
        """Return a new StrategyRuntimeConfig with patch fields applied."""
        data = self.model_dump()
        patch_data = patch.model_dump(exclude_none=True, exclude={"id"})
        data.update(patch_data)
        return StrategyRuntimeConfig(**data)

    def to_contract(self) -> StrategyConfig:
        """Convert to the contract-facing StrategyConfig shape."""
        return StrategyConfig(
            id=self.id,
            enabled=self.enabled,
            maxPositionSize=self.maxPositionSize,
            riskBudget=self.riskBudget,
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
