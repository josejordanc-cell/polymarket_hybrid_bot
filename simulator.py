"""
simulator.py
Paper-trading execution model.
Applies configurable slippage and tracks simulated fills for backtesting hooks.
"""
from __future__ import annotations

from datetime import datetime, timezone

import structlog

from utils.types import OrderIntent, TradeResult

logger = structlog.get_logger(__name__)


class PaperSimulator:
    """
    Simulates order execution with realistic slippage.
    Thread/coroutine-safe (stateless per-call; fill log is append-only).
    """

    def __init__(self, slippage_bps: int = 5) -> None:
        self._slippage = slippage_bps / 10_000
        self._fill_log: list[dict] = []

    async def execute(self, intent: OrderIntent) -> TradeResult:
        slipped_price = (
            intent.price * (1 + self._slippage)
            if intent.side == "BUY"
            else intent.price * (1 - self._slippage)
        )
        # Clamp to valid range
        slipped_price = max(0.001, min(0.999, slipped_price))

        result = TradeResult(
            accepted=True,
            order_id=f"paper-{intent.market_id}-{intent.strategy}-{intent.outcome}",
            fill_price=round(slipped_price, 6),
            filled_size=intent.size,
            fees_usd=0.0,  # Polymarket charges ~0.2% per side in live — leave for live executor
            reason="paper_fill",
        )

        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "market_id": intent.market_id,
            "strategy": intent.strategy,
            "side": intent.side,
            "outcome": intent.outcome,
            "price": intent.price,
            "fill_price": result.fill_price,
            "size": intent.size,
            "notional_usd": round(result.fill_price * intent.size, 4),  # type: ignore[operator]
        }
        self._fill_log.append(entry)
        logger.debug("paper_fill", **entry)
        return result

    # ------------------------------------------------------------------
    # Backtesting hooks
    # ------------------------------------------------------------------

    def get_fill_log(self) -> list[dict]:
        """Return a copy of all simulated fills for analysis."""
        return list(self._fill_log)

    def clear_fill_log(self) -> None:
        self._fill_log.clear()

    def summary(self) -> dict:
        """Aggregate fill statistics."""
        if not self._fill_log:
            return {"trades": 0, "total_notional_usd": 0.0}
        total_notional = sum(f["notional_usd"] for f in self._fill_log)
        by_strategy: dict[str, int] = {}
        for f in self._fill_log:
            by_strategy[f["strategy"]] = by_strategy.get(f["strategy"], 0) + 1
        return {
            "trades": len(self._fill_log),
            "total_notional_usd": round(total_notional, 2),
            "by_strategy": by_strategy,
        }

