"""tests/test_on_fill_strategy_merge.py

Unit tests for RiskManager.on_fill's existing-key BUY merge strategy
attribution (dominant-by-quantity, interim single-label fix).

A position keyed by ``{market_id}_{outcome}`` holds ONE Position with one
``strategy`` slot. When a second BUY from a different strategy merges onto
the same key, on_fill attributes the merged position to whichever side
contributed MORE quantity (incoming ``filled_size`` vs the PRE-merge
existing ``pos.quantity``); a tie keeps the existing label.

These tests use a real RiskManager. The P&L-guard test proves the strategy
edit does NOT perturb the financial fields (quantity / avg_price /
notional_usd) and that on_settlement's realised_pnl is strategy-agnostic.
"""
from __future__ import annotations

import pytest

from config.settings import (
    ApiConfig,
    BotConfig,
    RiskConfig,
    RuntimeConfig,
    StrategyConfig,
)
from risk_manager import RiskManager
from utils.types import OrderIntent


def _make_config(**kwargs: object) -> BotConfig:
    return BotConfig(
        api=ApiConfig(),
        risk=RiskConfig(),
        strategy=StrategyConfig(),
        runtime=RuntimeConfig(),
        **kwargs,
    )


def _intent(strategy: str, *, price: float, size: float) -> OrderIntent:
    """A BUY YES intent on a single shared market+outcome key."""
    return OrderIntent(
        market_id="0xmergemkt",
        token_id="yes",
        side="BUY",
        outcome="YES",
        size=size,
        price=price,
        strategy=strategy,
    )


_KEY = "0xmergemkt_YES"


def test_merge_second_larger_attributes_to_incoming() -> None:
    """Second BUY larger than the existing qty → strategy flips to incoming."""
    risk = RiskManager(_make_config())
    # A: 10 @ 0.40 ; B: 30 @ 0.60 (30 > 10 → dominant is B)
    risk.on_fill(_intent("A", price=0.40, size=10.0), fill_price=0.40, filled_size=10.0)
    risk.on_fill(_intent("B", price=0.60, size=30.0), fill_price=0.60, filled_size=30.0)

    pos = risk.open_positions[_KEY]
    assert pos.strategy == "B"
    assert pos.quantity == pytest.approx(40.0)
    # size-weighted avg = (0.40*10 + 0.60*30) / 40 = 0.55
    assert pos.avg_price == pytest.approx(0.55)


def test_merge_second_smaller_keeps_existing() -> None:
    """Second BUY smaller than the existing qty → strategy stays the opener's."""
    risk = RiskManager(_make_config())
    # A: 30 @ 0.40 ; B: 10 @ 0.60 (10 < 30 → dominant stays A)
    risk.on_fill(_intent("A", price=0.40, size=30.0), fill_price=0.40, filled_size=30.0)
    risk.on_fill(_intent("B", price=0.60, size=10.0), fill_price=0.60, filled_size=10.0)

    pos = risk.open_positions[_KEY]
    assert pos.strategy == "A"
    assert pos.quantity == pytest.approx(40.0)
    # (0.40*30 + 0.60*10) / 40 = 0.45
    assert pos.avg_price == pytest.approx(0.45)


def test_merge_equal_quantity_tie_keeps_existing() -> None:
    """Equal contribution is NOT a flip — tie keeps the existing label."""
    risk = RiskManager(_make_config())
    # A: 20 @ 0.50 ; B: 20 @ 0.50 (20 == 20 → strict > is False → keep A)
    risk.on_fill(_intent("A", price=0.50, size=20.0), fill_price=0.50, filled_size=20.0)
    risk.on_fill(_intent("B", price=0.50, size=20.0), fill_price=0.50, filled_size=20.0)

    pos = risk.open_positions[_KEY]
    assert pos.strategy == "A"
    assert pos.quantity == pytest.approx(40.0)
    assert pos.avg_price == pytest.approx(0.50)


def test_merge_pnl_fields_unperturbed_by_strategy_edit() -> None:
    """P&L guard: the strategy attribution must NOT touch quantity / avg_price
    / notional_usd, and on_settlement's realised_pnl must be identical to the
    strategy-agnostic arithmetic expectation."""
    risk = RiskManager(_make_config())
    # A: 10 @ 0.40 ; B: 30 @ 0.60 → merged qty 40, avg 0.55 (strategy → "B")
    risk.on_fill(_intent("A", price=0.40, size=10.0), fill_price=0.40, filled_size=10.0)
    risk.on_fill(_intent("B", price=0.60, size=30.0), fill_price=0.60, filled_size=30.0)

    pos = risk.open_positions[_KEY]
    # Strategy flipped...
    assert pos.strategy == "B"
    # ...but the financial fields are EXACTLY the arithmetic values.
    assert pos.quantity == pytest.approx(40.0)
    assert pos.avg_price == pytest.approx(0.55)
    assert pos.notional_usd == pytest.approx(40.0 * 0.55)  # 22.0

    # realised_pnl is strategy-agnostic: settle YES (win).
    # proceeds = quantity*1.0 = 40 ; pnl = proceeds - notional = 40 - 22 = 18.
    rp_before = risk.daily_pnl_snapshot().realised_pnl_usd
    risk.on_settlement("0xmergemkt", "YES")
    rp_after = risk.daily_pnl_snapshot().realised_pnl_usd
    assert rp_after - rp_before == pytest.approx(18.0)
