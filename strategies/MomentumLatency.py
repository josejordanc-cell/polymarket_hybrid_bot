from __future__ import annotations

from collections import deque

import numpy as np
import structlog

from config.settings import BotConfig
from utils.types import MarketSnapshot, OrderIntent, StrategySignal

logger = structlog.get_logger(__name__)


class MomentumLatency:
    name = "momentum_latency"

    def __init__(self, config: BotConfig) -> None:
        self._config = config
        self._window: deque[float] = deque(maxlen=50)

    def find_signal(self, snapshot: MarketSnapshot, spot_price: float | None) -> StrategySignal | None:
        if not self._config.strategy.momentum_enabled or spot_price is None:
            return None
        if snapshot.liquidity_usd < self._config.strategy.min_liquidity_usd:
            return None

        self._window.append(spot_price)
        if len(self._window) < 20:
            return None

        arr = np.array(self._window)
        zscore = (arr[-1] - arr.mean()) / (arr.std() + 1e-9)
        if abs(zscore) < self._config.strategy.momentum_z_threshold:
            return None

        side = "BUY_YES" if zscore > 0 else "BUY_NO"
        ref = snapshot.yes_ask if side == "BUY_YES" else snapshot.no_ask
        edge = min(0.10, abs(zscore) / 15)
        return StrategySignal(
            strategy_name=self.name,
            market_id=snapshot.market_id,
            side=side,
            confidence=min(1.0, abs(zscore) / 4),
            expected_edge=edge,
            reference_price=ref,
            reason=f"Spot momentum zscore={zscore:.2f} suggests lagging PM repricing",
        )

    def build_order(self, signal: StrategySignal, size_usd: float) -> OrderIntent:
        outcome = "YES" if signal.side == "BUY_YES" else "NO"
        qty = size_usd / max(signal.reference_price, 0.001)
        return OrderIntent(
            market_id=signal.market_id,
            side="BUY",
            outcome=outcome,
            size=qty,
            price=signal.reference_price,
            strategy=self.name,
        )
