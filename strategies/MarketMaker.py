from __future__ import annotations

import structlog

from config.settings import BotConfig
from utils.types import MarketSnapshot, OrderIntent

logger = structlog.get_logger(__name__)


class MarketMaker:
    name = "market_maker"

    def __init__(self, config: BotConfig) -> None:
        self._config = config

    def build_quotes(self, snapshot: MarketSnapshot, size_usd: float) -> list[OrderIntent]:
        if not self._config.strategy.market_maker_enabled:
            return []
        if snapshot.liquidity_usd < self._config.strategy.min_liquidity_usd:
            return []

        spread = self._config.strategy.mm_spread_bps / 10_000
        mid_yes = (snapshot.yes_bid + snapshot.yes_ask) / 2
        bid = max(0.01, mid_yes - spread / 2)
        ask = min(0.99, mid_yes + spread / 2)

        qty_bid = size_usd / max(bid, 0.001)
        qty_ask = size_usd / max(ask, 0.001)
        return [
            OrderIntent(
                market_id=snapshot.market_id,
                side="BUY",
                outcome="YES",
                size=qty_bid,
                price=bid,
                tif="GTC",
                strategy=self.name,
            ),
            OrderIntent(
                market_id=snapshot.market_id,
                side="SELL",
                outcome="YES",
                size=qty_ask,
                price=ask,
                tif="GTC",
                strategy=self.name,
            ),
        ]
