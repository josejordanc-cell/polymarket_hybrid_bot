"""
strategies/market_maker.py
Inventory-aware market making: quote tight bid/ask spreads on both sides,
manage net inventory to stay delta-neutral, capture rebates.

Priority: 3 — runs when no higher-priority signal is found.
"""
from __future__ import annotations

from typing import Any

import structlog

from config.settings import BotConfig
from utils.math import round_tick
from utils.types import MarketSnapshot, OrderIntent

logger = structlog.get_logger(__name__)

try:
    from py_clob_client.clob_types import OrderArgs
    from py_clob_client.order_builder.constants import Side
    _CLOB_AVAILABLE = True
except Exception:
    OrderArgs = dict  # type: ignore[assignment,misc]

    class Side:  # type: ignore[no-redef]
        BUY = "BUY"
        SELL = "SELL"

    _CLOB_AVAILABLE = False



class MarketMakerStrategy:
    name = "market_maker"

    def __init__(self, config: BotConfig) -> None:
        self._config = config
        # Track inventory per market: positive = net YES, negative = net NO
        self._inventory: dict[str, float] = {}

    # ------------------------------------------------------------------
    # Quote generation
    # ------------------------------------------------------------------

    def build_quotes(
        self, snapshot: MarketSnapshot, size_usd: float
    ) -> list[OrderIntent]:
        """
        Generate GTC bid and ask quotes around the YES midpoint.

        Inventory skew: if we're net long YES, tighten ask / widen bid
        to nudge back toward neutral.
        """
        sc = self._config.strategy
        if not sc.market_maker_enabled:
            return []
        if snapshot.liquidity_usd < sc.min_liquidity_usd:
            return []
        if size_usd <= 0:
            return []

        spread = sc.mm_spread_bps / 10_000
        mid = snapshot.mid_yes

        # Inventory skew: shift quotes to offload excess inventory
        inv = self._inventory.get(snapshot.market_id, 0.0)
        max_inv = size_usd / max(mid, 0.01)
        skew = (inv / max(max_inv, 1.0)) * (spread / 2)

        bid = round_tick(
            max(0.01, min(0.98, mid - spread / 2 - skew)), snapshot.tick_size
        )
        ask = round_tick(
            max(0.02, min(0.99, mid + spread / 2 - skew)), snapshot.tick_size
        )

        if ask <= bid:
            # Safety: ensure non-crossing quotes
            ask = round_tick(bid + snapshot.tick_size, snapshot.tick_size)

        qty_bid = round(size_usd / max(bid, 0.001), 6)
        qty_ask = round(size_usd / max(ask, 0.001), 6)

        return [
            OrderIntent(
                market_id=snapshot.market_id,
                token_id=snapshot.yes_token_id,
                side="BUY",
                outcome="YES",
                size=qty_bid,
                price=bid,
                tick_size=snapshot.tick_size,
                tif="GTC",
                strategy=self.name,
            ),
            OrderIntent(
                market_id=snapshot.market_id,
                token_id=snapshot.yes_token_id,
                side="SELL",
                outcome="YES",
                size=qty_ask,
                price=ask,
                tick_size=snapshot.tick_size,
                tif="GTC",
                strategy=self.name,
            ),
        ]

    # ------------------------------------------------------------------
    # Inventory tracking
    # ------------------------------------------------------------------

    def on_fill(self, intent: OrderIntent, filled_size: float) -> None:
        """Update inventory after a confirmed fill."""
        mkt = intent.market_id
        delta = filled_size if intent.side == "BUY" else -filled_size
        self._inventory[mkt] = self._inventory.get(mkt, 0.0) + delta

    def get_inventory(self, market_id: str) -> float:
        return self._inventory.get(market_id, 0.0)

    # ------------------------------------------------------------------
    # Live order adapter
    # ------------------------------------------------------------------

    def to_live_order_args(self, intent: OrderIntent) -> Any:
        if not _CLOB_AVAILABLE:
            raise RuntimeError("py-clob-client-v2 is not installed")
        return OrderArgs(
            token_id=intent.token_id,
            price=intent.price_rounded(),
            size=intent.size,
            side=Side.BUY if intent.side == "BUY" else Side.SELL,
        )

