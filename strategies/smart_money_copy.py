"""
strategies/smart_money_copy.py
Optional Layer 4: on-chain smart-money copy trading.

Monitors high-win-rate wallets via Polygon RPC and mirrors their
Polymarket positions with risk caps. DISABLED by default.

Enable via: BOT_STRAT_SMART_MONEY_ENABLED=true in .env
"""
from __future__ import annotations

from typing import Any

import structlog

from config.settings import BotConfig
from data_feeds.polygon_feed import WalletScore
from utils.math import round_tick
from utils.types import MarketSnapshot, OrderIntent, StrategySignal

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



class SmartMoneyCopyStrategy:
    name = "smart_money_copy"

    def __init__(self, config: BotConfig) -> None:
        self._config = config

    # ------------------------------------------------------------------
    # Signal detection
    # ------------------------------------------------------------------

    def find_signal(
        self,
        snapshot: MarketSnapshot,
        wallets: list[WalletScore],
    ) -> StrategySignal | None:
        sc = self._config.strategy
        if not sc.smart_money_enabled:
            return None
        if not wallets:
            return None
        if snapshot.liquidity_usd < sc.min_liquidity_usd:
            return None

        # Pick wallet with best combined score
        qualified = [
            w for w in wallets
            if w.win_rate >= sc.smart_money_min_win_rate
            and w.profit_factor >= sc.smart_money_min_profit_factor
        ]
        if not qualified:
            return None

        best = max(qualified, key=lambda w: w.win_rate * w.profit_factor)

        # Confidence: excess win rate × profit factor, capped at 0.85
        confidence = min(0.85, (best.win_rate - 0.50) * best.profit_factor)
        if confidence < 0.35:
            return None

        # Conservative edge: max 6% to stay below momentum cap
        expected_edge = min(0.06, confidence * 0.5)

        logger.info(
            "smart_money_signal",
            market=snapshot.market_id,
            wallet=best.wallet,
            win_rate=round(best.win_rate, 3),
            profit_factor=round(best.profit_factor, 3),
            confidence=round(confidence, 3),
        )

        return StrategySignal(
            strategy_name=self.name,
            market_id=snapshot.market_id,
            side="BUY_YES",
            confidence=confidence,
            expected_edge=expected_edge,
            reference_price=snapshot.yes_ask,
            reason=(
                f"Mirror wallet={best.wallet[:10]}… "
                f"wr={best.win_rate:.2f} pf={best.profit_factor:.2f}"
            ),
        )

    # ------------------------------------------------------------------
    # Order building
    # ------------------------------------------------------------------

    def build_order(
        self,
        signal: StrategySignal,
        size_usd: float,
        snapshot: MarketSnapshot,
    ) -> OrderIntent | None:
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
    # Live adapter
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

