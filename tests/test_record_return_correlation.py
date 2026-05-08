"""Prompt 5: record_return wiring and correlation guard integration."""
from __future__ import annotations

from unittest.mock import patch

import numpy as np

from config import get_config
from risk_manager import RiskManager
from utils.fill_correlation import record_fill_return_for_correlation
from utils.types import MarketSnapshot, OrderIntent


def _minimal_snapshot(market_id: str = "mkt-a") -> MarketSnapshot:
    return MarketSnapshot(
        market_id=market_id,
        yes_bid=0.44,
        yes_ask=0.46,
        no_bid=0.53,
        no_ask=0.55,
        liquidity_usd=10_000.0,
        tick_size=0.01,
        yes_token_id="yes",
        no_token_id="no",
    )


def test_record_fill_return_invokes_record_return_with_computed_value() -> None:
    cfg = get_config()
    risk = RiskManager(cfg)
    snap = _minimal_snapshot()
    intent = OrderIntent(
        market_id=snap.market_id,
        token_id="yes",
        side="BUY",
        outcome="YES",
        size=10.0,
        price=0.46,
        strategy="momentum_latency",
        signal_reference_price=0.45,
    )
    with patch.object(risk, "record_return", wraps=risk.record_return) as spy:
        record_fill_return_for_correlation(risk, intent, snap, 0.46, 5.0)
    spy.assert_called_once()
    args, kwargs = spy.call_args
    assert args[0] == snap.market_id
    # BUY, fill 0.46 vs ref 0.45 → positive return
    assert args[1] > 0
    assert len(risk._return_history[snap.market_id]) == 1


def test_record_fill_return_skips_zero_size() -> None:
    cfg = get_config()
    risk = RiskManager(cfg)
    snap = _minimal_snapshot()
    intent = OrderIntent(
        market_id=snap.market_id,
        token_id="yes",
        side="BUY",
        outcome="YES",
        size=10.0,
        price=0.46,
        strategy="market_maker",
    )
    with patch.object(risk, "record_return") as spy:
        record_fill_return_for_correlation(risk, intent, snap, 0.46, 0.0)
    spy.assert_not_called()


def test_approve_orders_reaches_correlation_guard() -> None:
    """Assert correlation_blocked is consulted and actually blocks a correlated intent.

    Sets up correlated return histories and an open position for mkt-open so
    that correlation_blocked("mkt-new") returns True post-fix.  Confirms:
    1. The guard is called with the correct market_id.
    2. The intent is absent from the approved list (guard rejected it).
    """
    rng = np.random.default_rng(42)
    base = rng.standard_normal(25)
    noisy = base + rng.standard_normal(25) * 0.01

    cfg = get_config()
    risk = RiskManager(cfg)

    for v in base:
        risk.record_return("mkt-open", float(v))
    for v in noisy:
        risk.record_return("mkt-new", float(v))

    open_intent = OrderIntent(
        market_id="mkt-open",
        token_id="yes",
        side="BUY",
        outcome="YES",
        size=10.0,
        price=0.50,
        strategy="momentum_latency",
    )
    risk.on_fill(open_intent, 0.50, 10.0)

    intent = OrderIntent(
        market_id="mkt-new",
        token_id="yes",
        side="BUY",
        outcome="YES",
        size=100.0,
        price=0.50,
        strategy="momentum_latency",
    )
    with patch.object(
        risk,
        "correlation_blocked",
        wraps=risk.correlation_blocked,
    ) as guard:
        approved = risk.approve_orders([intent])
    guard.assert_called()
    assert guard.call_args[0][0] == "mkt-new"
    assert approved == []
