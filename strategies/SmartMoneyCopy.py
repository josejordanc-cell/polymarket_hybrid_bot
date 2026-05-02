from __future__ import annotations

import structlog

from config.settings import BotConfig
from data_feeds.polygon_feed import WalletScore
from utils.types import MarketSnapshot, OrderIntent, StrategySignal

logger = structlog.get_logger(__name__)


class SmartMoneyCopy:
    name = "smart_money_copy"

    def __init__(self, config: BotConfig) -> None:
        self._config = config

    def find_signal(
        self, snapshot: MarketSnapshot, wallets: list[WalletScore]
    ) -> StrategySignal | None:
        if not self._config.strategy.smart_money_enabled or not wallets:
            return None
        if snapshot.liquidity_usd < self._config.strategy.min_liquidity_usd:
            return None

        best = max(wallets, key=lambda w: (w.profit_factor, w.win_rate))
        confidence = min(1.0, (best.win_rate - 0.5) * best.profit_factor)
        if confidence < 0.4:
            return None

        return StrategySignal(
            strategy_name=self.name,
            market_id=snapshot.market_id,
            side="BUY_YES",
            confidence=confidence,
            expected_edge=min(0.08, confidence / 8),
            reference_price=snapshot.yes_ask,
            reason=f"Mirror wallet={best.wallet} wr={best.win_rate:.2f} pf={best.profit_factor:.2f}",
        )

    def build_order(self, signal: StrategySignal, size_usd: float) -> OrderIntent:
        qty = size_usd / max(signal.reference_price, 0.001)
        return OrderIntent(
            market_id=signal.market_id,
            side="BUY",
            outcome="YES",
            size=qty,
            price=signal.reference_price,
            strategy=self.name,
        )
