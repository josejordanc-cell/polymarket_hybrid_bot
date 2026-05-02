from __future__ import annotations

import structlog

from config.settings import BotConfig
from utils.types import MarketSnapshot, OrderIntent, StrategySignal

logger = structlog.get_logger(__name__)


class IntraArbitrage:
    name = "intra_arbitrage"

    def __init__(self, config: BotConfig) -> None:
        self._config = config

    def find_signal(self, snapshot: MarketSnapshot) -> StrategySignal | None:
        if snapshot.liquidity_usd < self._config.strategy.min_liquidity_usd:
            return None

        combined = snapshot.yes_ask + snapshot.no_ask
        edge = 1.0 - combined
        if edge < self._config.strategy.min_arb_edge:
            return None

        logger.info("arb_detected", market_id=snapshot.market_id, edge=edge, combined=combined)
        return StrategySignal(
            strategy_name=self.name,
            market_id=snapshot.market_id,
            side="BUY_YES",
            confidence=1.0,
            expected_edge=edge,
            reference_price=min(snapshot.yes_ask, snapshot.no_ask),
            reason=f"Guaranteed edge from YES+NO={combined:.4f}",
        )

    def build_orders(self, snapshot: MarketSnapshot, size_usd: float) -> list[OrderIntent]:
        # <arbitrage_bundle>
        # Buy YES and NO simultaneously to lock payout = 1.00.
        # </arbitrage_bundle>
        yes_qty = size_usd / max(snapshot.yes_ask, 0.001)
        no_qty = size_usd / max(snapshot.no_ask, 0.001)
        return [
            OrderIntent(
                market_id=snapshot.market_id,
                side="BUY",
                outcome="YES",
                size=yes_qty,
                price=snapshot.yes_ask,
                strategy=self.name,
            ),
            OrderIntent(
                market_id=snapshot.market_id,
                side="BUY",
                outcome="NO",
                size=no_qty,
                price=snapshot.no_ask,
                strategy=self.name,
            ),
        ]
