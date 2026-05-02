"""
utils/types.py
Shared, fully-typed Pydantic models used across the bot.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------

class MarketSnapshot(BaseModel):
    """Live orderbook snapshot from the Polymarket CLOB feed."""
    market_id: str
    yes_bid: float = Field(ge=0.0, le=1.0)
    yes_ask: float = Field(ge=0.0, le=1.0)
    no_bid: float = Field(ge=0.0, le=1.0)
    no_ask: float = Field(ge=0.0, le=1.0)
    liquidity_usd: float = Field(ge=0.0)
    tick_size: float = Field(default=0.01, gt=0)
    # Token IDs used by py-clob-client-v2 for order submission
    yes_token_id: str = Field(default="")
    no_token_id: str = Field(default="")
    updated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def _sane_spread(self) -> "MarketSnapshot":
        # Soft-guard: ask must be >= bid (can happen on stale data)
        if self.yes_ask < self.yes_bid:
            object.__setattr__(self, "yes_ask", self.yes_bid + self.tick_size)
        if self.no_ask < self.no_bid:
            object.__setattr__(self, "no_ask", self.no_bid + self.tick_size)
        return self

    @property
    def mid_yes(self) -> float:
        return (self.yes_bid + self.yes_ask) / 2

    @property
    def mid_no(self) -> float:
        return (self.no_bid + self.no_ask) / 2

    @property
    def implied_sum(self) -> float:
        """YES ask + NO ask — below 1.0 indicates an arbitrage opportunity."""
        return self.yes_ask + self.no_ask


class SpotTick(BaseModel):
    """Real-time spot price tick from a centralised exchange."""
    venue: Literal["binance", "coinbase"]
    symbol: str
    price: float = Field(gt=0)
    ts: datetime = Field(default_factory=utc_now)


# ---------------------------------------------------------------------------
# Positions & signals
# ---------------------------------------------------------------------------

class Position(BaseModel):
    """Open position held by the bot."""
    market_id: str
    outcome: Literal["YES", "NO"]
    quantity: float = Field(gt=0)
    avg_price: float = Field(gt=0, le=1.0)
    strategy: str
    opened_at: datetime = Field(default_factory=utc_now)

    @property
    def notional_usd(self) -> float:
        return self.quantity * self.avg_price

    @property
    def unrealised_pnl(self, current_price: float = 0.0) -> float:
        """Approximate unrealised PnL given a mark price."""
        return self.quantity * (current_price - self.avg_price)


class StrategySignal(BaseModel):
    """Trading signal produced by a strategy."""
    strategy_name: str
    market_id: str
    side: Literal["BUY_YES", "BUY_NO", "MM_BID", "MM_ASK"]
    confidence: float = Field(ge=0.0, le=1.0)
    expected_edge: float = Field(ge=0.0, le=1.0)
    reference_price: float = Field(gt=0.0, le=1.0)
    reason: str
    ts: datetime = Field(default_factory=utc_now)


class OrderIntent(BaseModel):
    """Fully-resolved order ready for risk approval and execution."""
    market_id: str
    token_id: str = Field(default="")          # py-clob-client-v2 requires token_id
    side: Literal["BUY", "SELL"]
    outcome: Literal["YES", "NO"]
    size: float = Field(gt=0)
    price: float = Field(gt=0, le=1.0)
    tif: Literal["GTC", "IOC", "FOK"] = "IOC"
    strategy: str
    tick_size: float = Field(default=0.01, gt=0)
    bundle_id: Optional[str] = None
    # StrategySignal.reference_price at intent creation; None → book mid at fill (Prompt 5)
    signal_reference_price: Optional[float] = Field(default=None)

    @property
    def notional_usd(self) -> float:
        return self.size * self.price

    def price_rounded(self) -> float:
        """Return price snapped to exchange tick_size."""
        return round(round(self.price / self.tick_size) * self.tick_size, 6)


class TradeResult(BaseModel):
    """Outcome of an order submission (live or simulated)."""
    accepted: bool
    order_id: Optional[str] = None
    fill_price: Optional[float] = None
    filled_size: float = 0.0
    fees_usd: float = 0.0
    reason: Optional[str] = None
    ts: datetime = Field(default_factory=utc_now)

    @property
    def realised_cost_usd(self) -> float:
        base = (self.fill_price or 0.0) * self.filled_size
        return base + self.fees_usd


# ---------------------------------------------------------------------------
# PnL accounting
# ---------------------------------------------------------------------------

class DailyPnL(BaseModel):
    """Intraday PnL tracker snapshot."""
    date_str: str
    start_equity_usd: float
    realised_pnl_usd: float = 0.0
    unrealised_pnl_usd: float = 0.0
    num_trades: int = 0

    @property
    def total_pnl_usd(self) -> float:
        return self.realised_pnl_usd + self.unrealised_pnl_usd

    @property
    def drawdown_pct(self) -> float:
        if self.start_equity_usd <= 0:
            return 0.0
        loss = max(0.0, -self.total_pnl_usd)
        return loss / self.start_equity_usd

