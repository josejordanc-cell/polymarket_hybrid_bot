"""
T5: Unit tests for correlation_blocked key-shape fix.

Verifies that correlation_blocked correctly iterates Position.market_id
from open_positions.values() (bare market IDs) rather than composite
keys ({market_id}_{outcome}) which would never match _return_history.
"""
from __future__ import annotations

import numpy as np

from config import get_config
from risk_manager import RiskManager
from utils.types import OrderIntent


def _make_intent(market_id: str) -> OrderIntent:
    return OrderIntent(
        market_id=market_id,
        token_id="yes",
        side="BUY",
        outcome="YES",
        size=10.0,
        price=0.50,
        strategy="momentum_latency",
    )


def test_correlation_blocked_true_when_open_market_correlated() -> None:
    """Smoking-gun test: guard must fire when an open market is correlated.

    This test MUST fail on a clean revert of the production fix because the old
    code iterated composite keys ({market_id}_{outcome}) against _return_history
    which is keyed by bare market_id -- all lookups returned [] so the guard was
    a silent no-op.
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

    intent_open = _make_intent("mkt-open")
    risk.on_fill(intent_open, 0.50, 10.0)

    assert "mkt-open_YES" in risk.open_positions

    assert risk.correlation_blocked("mkt-new") is True


def test_correlation_blocked_false_when_uncorrelated() -> None:
    """Guard must not fire when the open market returns are independent."""
    rng_open = np.random.default_rng(0)
    rng_new = np.random.default_rng(1)
    series_open = rng_open.standard_normal(25)
    series_new = rng_new.standard_normal(25)

    cfg = get_config()
    risk = RiskManager(cfg)

    for v in series_open:
        risk.record_return("mkt-open", float(v))
    for v in series_new:
        risk.record_return("mkt-new", float(v))

    intent_open = _make_intent("mkt-open")
    risk.on_fill(intent_open, 0.50, 10.0)

    assert risk.correlation_blocked("mkt-new") is False


def test_correlation_blocked_false_when_insufficient_samples() -> None:
    """Line-195 short-circuit: fewer than 20 samples for the candidate market."""
    rng = np.random.default_rng(7)
    series_open = rng.standard_normal(25)
    series_new = rng.standard_normal(10)  # only 10 samples -- below the 20 threshold

    cfg = get_config()
    risk = RiskManager(cfg)

    for v in series_open:
        risk.record_return("mkt-open", float(v))
    for v in series_new:
        risk.record_return("mkt-new", float(v))

    intent_open = _make_intent("mkt-open")
    risk.on_fill(intent_open, 0.50, 10.0)

    assert risk.correlation_blocked("mkt-new") is False


def test_correlation_blocked_false_for_self_market() -> None:
    """A market must not be blocked against itself.

    When the only open position is for mkt-x and we query mkt-x,
    the set comprehension excludes self and no candidates remain.
    """
    rng = np.random.default_rng(99)
    series = rng.standard_normal(25)

    cfg = get_config()
    risk = RiskManager(cfg)

    for v in series:
        risk.record_return("mkt-x", float(v))

    intent = _make_intent("mkt-x")
    risk.on_fill(intent, 0.50, 10.0)

    assert risk.correlation_blocked("mkt-x") is False
