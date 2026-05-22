"""
strategies/momentum_latency.py
Spot-vs-Polymarket latency arbitrage for 5-min and 15-min contracts.

Logic:
  1. Measure z-score of latest spot return vs rolling window.
  2. If |z| > threshold, assume Polymarket pricing LAGS spot momentum.
  3. Buy YES if spot is strongly up; buy NO if strongly down.
  4. Apply Kelly sizing from RiskManager.

Priority: 2 — runs only when no intra-arb signal is found.
"""
from __future__ import annotations

from typing import Any

import numpy as np
import structlog

from config.settings import BotConfig
from utils.math import round_tick
from utils.types import MarketSnapshot, OrderIntent, StrategySignal

logger = structlog.get_logger(__name__)

try:
    from py_clob_client.clob_types import OrderArgs
    _CLOB_AVAILABLE = True
except Exception:
    OrderArgs = dict  # type: ignore[assignment,misc]
    _CLOB_AVAILABLE = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------

class MomentumLatencyStrategy:
    name = "momentum_latency"

    def __init__(self, config: BotConfig) -> None:
        self._config = config

    # ------------------------------------------------------------------
    # Signal detection
    # ------------------------------------------------------------------

    def find_signal(
        self,
        snapshot: MarketSnapshot,
        symbol: str,
        return_series: np.ndarray,
    ) -> StrategySignal | None:
        """
        Produce a signal if spot momentum z-score exceeds threshold.

        Args:
            snapshot:      Live Polymarket orderbook snapshot.
            symbol:        Spot symbol (e.g. 'BTC/USDT').
            return_series: Log-return array from SpotFeed.get_return_series().
        """
        sc = self._config.strategy
        if not sc.momentum_enabled:
            return None
        if snapshot.liquidity_usd < sc.min_liquidity_usd:
            return None
        if len(return_series) < max(sc.momentum_window, 20):
            return None

        window = sc.momentum_window
        rets = return_series[-window:]
        mean = np.mean(rets)
        std = np.std(rets)
        if std < 1e-10:
            return None

        z = float((rets[-1] - mean) / std)
        if abs(z) < sc.momentum_z_threshold:
            return None

        side = "BUY_YES" if z > 0 else "BUY_NO"
        ref = snapshot.yes_ask if side == "BUY_YES" else snapshot.no_ask

        # Conservative edge estimate: scales with z but capped at 8%
        raw_edge = abs(z) / (sc.momentum_z_threshold * 10)
        expected_edge = float(np.clip(raw_edge, 0.0, 0.08))

        # Confidence: maps |z| to [0.50, 0.95]
        confidence = float(np.clip(0.50 + abs(z) / (sc.momentum_z_threshold * 6), 0.50, 0.95))

        logger.info(
            "momentum_signal",
            market_id=snapshot.market_id,
            symbol=symbol,
            z_score=round(z, 3),
            side=side,
            edge=round(expected_edge, 4),
            confidence=round(confidence, 3),
        )

        return StrategySignal(
            strategy_name=self.name,
            market_id=snapshot.market_id,
            side=side,
            confidence=confidence,
            expected_edge=expected_edge,
            reference_price=ref,
            reason=(
                f"{symbol} momentum z={z:.2f} (window={window}); "
                f"Polymarket lag → {side}"
            ),
        )

    # ------------------------------------------------------------------
    # Order building
    # ------------------------------------------------------------------

    def build_order(
        self, signal: StrategySignal, size_usd: float, snapshot: MarketSnapshot
    ) -> OrderIntent | None:
        """Build a single IOC market-side order for the signal direction."""
        if size_usd <= 0:
            return None

        outcome = "YES" if signal.side == "BUY_YES" else "NO"
        price = round_tick(signal.reference_price, snapshot.tick_size)
        qty = round(size_usd / max(price, 0.001), 6)
        token_id = snapshot.yes_token_id if outcome == "YES" else snapshot.no_token_id

        return OrderIntent(
            market_id=signal.market_id,
            token_id=token_id,
            side="BUY",
            outcome=outcome,
            size=qty,
            price=price,
            tick_size=snapshot.tick_size,
            tif="IOC",
            strategy=self.name,
            signal_reference_price=signal.reference_price,
        )

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
            side=intent.side,
        )

