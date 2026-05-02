"""
Unit tests for intra-arbitrage bundle reconciliation (Prompt 3).
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from config.settings import BotConfig, StrategyConfig
from strategies.intra_arbitrage import reconcile_arb_bundle
from utils.types import MarketSnapshot, OrderIntent, TradeResult


@pytest.mark.asyncio
async def test_imbalance_yes_filled_no_zero_issues_sell_yes() -> None:
    """YES leg 100 shares, NO leg 0 → compensating IOC SELL 100 YES."""
    config = BotConfig(
        strategy=StrategyConfig(arb_max_unwind_bps=5000),
    )

    snap = MarketSnapshot(
        market_id="m1",
        yes_bid=0.48,
        yes_ask=0.50,
        no_bid=0.48,
        no_ask=0.50,
        liquidity_usd=10_000.0,
        tick_size=0.01,
        yes_token_id="tok_yes",
        no_token_id="tok_no",
    )

    bundle = "test-bundle-1"
    yes_intent = OrderIntent(
        market_id="m1",
        token_id="tok_yes",
        side="BUY",
        outcome="YES",
        size=100.0,
        price=0.50,
        tick_size=0.01,
        tif="IOC",
        strategy="intra_arbitrage",
        bundle_id=bundle,
    )
    no_intent = OrderIntent(
        market_id="m1",
        token_id="tok_no",
        side="BUY",
        outcome="NO",
        size=100.0,
        price=0.50,
        tick_size=0.01,
        tif="IOC",
        strategy="intra_arbitrage",
        bundle_id=bundle,
    )

    yes_result = TradeResult(
        accepted=True,
        fill_price=0.50,
        filled_size=100.0,
        reason="matched",
    )
    no_result = TradeResult(
        accepted=True,
        fill_price=0.0,
        filled_size=0.0,
        reason="canceled_no_fill",
    )

    recorded: list[OrderIntent] = []

    class _Exec:
        async def execute(self, intent: OrderIntent) -> TradeResult:
            recorded.append(intent)
            return TradeResult(
                accepted=True,
                fill_price=0.48,
                filled_size=intent.size,
                reason="matched",
            )

    risk = MagicMock()
    mm = MagicMock()

    await reconcile_arb_bundle(
        config,
        _Exec(),
        snap,
        bundle,
        yes_intent,
        no_intent,
        yes_result,
        no_result,
        risk,
        mm,
    )

    assert len(recorded) == 1
    u = recorded[0]
    assert u.side == "SELL"
    assert u.outcome == "YES"
    assert abs(u.size - 100.0) < 1e-6
    assert u.token_id == "tok_yes"
    assert u.bundle_id is None
    risk.on_fill.assert_called_once()
    mm.on_fill.assert_called_once()
