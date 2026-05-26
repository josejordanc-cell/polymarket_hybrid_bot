"""tests/test_recent_fills_mirror.py

Unit tests for utils.fills.append_state_fill_record (profitability epic
piece (2a)). Targets the helper directly; main.py:_exec_and_record runtime
wiring is helper-unit-covered but runtime-unverified until the pending
droplet redeploy + sim smoke — recorded as an owed gap in
.swarm-decisions b1-fill-runtime-deferred.
"""
from __future__ import annotations

import asyncio
from collections import deque

import pytest

from api.schemas import FillRecord, StrategyRuntimeConfig
from api.state import BotState
from utils.fills import append_state_fill_record


def _fresh_state() -> BotState:
    return BotState.create(
        equity=1000.0,
        day_start_equity=1000.0,
        open_positions={},
    )


@pytest.mark.asyncio
async def test_append_produces_fillrecord() -> None:
    state = _fresh_state()
    await append_state_fill_record(
        state,
        market_id="0xabcdef0123456789",
        outcome="YES",
        fill_price=0.5,
        filled_size=10.0,
        strategy="intra_arbitrage",
    )
    assert len(state.recent_fills) == 1
    rec = state.recent_fills[0]
    assert isinstance(rec, FillRecord)
    assert rec.symbol == "0xabcdef0123"  # market_id[:12]
    assert rec.action == "UP"
    assert rec.price == 0.5
    assert rec.size == 10.0
    assert rec.status == "filled"
    assert rec.strategy == "strategy1"  # bot_to_dashboard("intra_arbitrage")
    assert rec.pnl is None


@pytest.mark.asyncio
async def test_outcome_no_emits_down() -> None:
    state = _fresh_state()
    await append_state_fill_record(
        state, market_id="0xabc", outcome="NO",
        fill_price=0.5, filled_size=10.0, strategy="momentum_latency",
    )
    assert state.recent_fills[0].action == "DOWN"
    assert state.recent_fills[0].strategy == "strategy2"


@pytest.mark.asyncio
async def test_appendleft_ordering() -> None:
    """Newest at left per api/state.py:20 invariant."""
    state = _fresh_state()
    for i in range(3):
        await append_state_fill_record(
            state, market_id=f"0x{i:012d}", outcome="YES",
            fill_price=float(i), filled_size=1.0, strategy="intra_arbitrage",
        )
    # Newest (i=2) should be at index 0, oldest (i=0) at index 2
    assert state.recent_fills[0].price == 2.0
    assert state.recent_fills[1].price == 1.0
    assert state.recent_fills[2].price == 0.0


@pytest.mark.asyncio
async def test_maxlen_enforced() -> None:
    """recent_fills deque maxlen=200; the 201st append drops the oldest."""
    state = _fresh_state()
    for i in range(201):
        await append_state_fill_record(
            state, market_id=f"0x{i:012d}", outcome="YES",
            fill_price=float(i), filled_size=1.0, strategy="intra_arbitrage",
        )
    assert len(state.recent_fills) == 200
    # Newest (i=200) at left; oldest retained is i=1 at the right
    assert state.recent_fills[0].price == 200.0
    assert state.recent_fills[-1].price == 1.0


@pytest.mark.asyncio
async def test_smart_money_swallowed_no_mutation() -> None:
    """smart_money_copy has no v1.0 contract id; bot_to_dashboard raises,
    helper catches, state stays empty."""
    state = _fresh_state()
    await append_state_fill_record(
        state, market_id="0xabc", outcome="YES",
        fill_price=0.5, filled_size=10.0, strategy="smart_money_copy",
    )
    assert len(state.recent_fills) == 0


@pytest.mark.asyncio
async def test_write_lock_acquired() -> None:
    """Helper acquires state.write_lock during append; concurrent appends
    don't interleave-corrupt the deque."""
    state = _fresh_state()

    async def _one(i: int) -> None:
        await append_state_fill_record(
            state, market_id=f"0x{i:012d}", outcome="YES",
            fill_price=float(i), filled_size=1.0, strategy="intra_arbitrage",
        )

    await asyncio.gather(*(_one(i) for i in range(20)))
    assert len(state.recent_fills) == 20
    prices = {rec.price for rec in state.recent_fills}
    assert prices == {float(i) for i in range(20)}
