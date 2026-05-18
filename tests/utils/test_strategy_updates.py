"""
tests/utils/test_strategy_updates.py — D.2.4 step 3 STRATEGY_UPDATE
broadcast wire-up tests.

Verifies that utils.strategy_updates.broadcast_strategy_update
broadcasts a flat StrategyUpdate-shaped payload through the registered
Emitter, per the contract reconciled with the dashboard zod schema in
this batch.

Structure mirrors tests/utils/test_fills.py from the FILL batch. Note
one structural difference: broadcast_strategy_update is SYNC (because
apply_strategy_patch is sync), so it schedules em.broadcast() as a
task on the running event loop. Tests are async to provide a running
loop; the broadcast call site is sync but yields control back via
``await asyncio.sleep(0)`` for the scheduled task to run.

See docs/audit/d24-emit-site-audit.md §5.3 (gate) and §10
(STRATEGY_UPDATE drift discovery) for context.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.emit import Emitter
from utils import strategy_updates as strategy_updates_mod
from utils.strategy_updates import broadcast_strategy_update, set_emitter


@pytest.fixture(autouse=True)
def _reset_emitter():
    """Clear the module-level emitter before and after every test."""
    set_emitter(None)
    yield
    set_emitter(None)


def _make_mock_emitter() -> MagicMock:
    em = MagicMock(spec=Emitter)
    em.broadcast = AsyncMock()
    return em


@pytest.mark.asyncio
async def test_broadcast_strategy_update_broadcasts_when_emitter_registered() -> None:
    em = _make_mock_emitter()
    set_emitter(em)

    broadcast_strategy_update(
        strategy_id="strategy1",
        updates={"enabled": False, "maxPositionSize": 75.0},
    )

    # Sync function schedules an async task; let it run.
    await asyncio.sleep(0)

    assert em.broadcast.await_count == 1
    msg = em.broadcast.await_args.args[0]
    assert msg["type"] == "STRATEGY_UPDATE"
    payload = msg["payload"]
    assert set(payload.keys()) == {"strategyId", "updates"}
    assert payload["strategyId"] == "strategy1"
    assert payload["updates"] == {"enabled": False, "maxPositionSize": 75.0}


@pytest.mark.asyncio
async def test_broadcast_strategy_update_is_noop_when_no_emitter() -> None:
    # No set_emitter() call; _reset_emitter fixture ensures it is None.
    assert strategy_updates_mod._emitter is None
    # Must not raise.
    broadcast_strategy_update(strategy_id="strategy2", updates={"enabled": True})
    await asyncio.sleep(0)
    # Nothing to assert beyond "didn't raise" — fixture verifies state.


def test_broadcast_strategy_update_is_noop_in_sync_context_without_loop() -> None:
    """When called from sync test code with NO event loop running (the
    typical pattern in tests/api/test_state.py apply_patch tests), the
    broadcast silently no-ops rather than raising RuntimeError. This is
    the intended semantics — sync tests of state mutation shouldn't have
    to set up an event loop just to suppress the broadcast."""
    em = _make_mock_emitter()
    set_emitter(em)

    # No event loop here — this is a sync test, not async.
    broadcast_strategy_update(strategy_id="strategy1", updates={"enabled": False})

    # broadcast should never have been called.
    assert em.broadcast.await_count == 0


@pytest.mark.asyncio
async def test_broadcast_strategy_update_handles_multiple_calls() -> None:
    em = _make_mock_emitter()
    set_emitter(em)

    broadcast_strategy_update(strategy_id="strategy1", updates={"enabled": True})
    broadcast_strategy_update(strategy_id="strategy2", updates={"maxPositionSize": 50.0})
    broadcast_strategy_update(strategy_id="strategy3", updates={"riskBudget": 0.5})

    # Let all three scheduled tasks run.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert em.broadcast.await_count == 3
    seen_ids = [
        call.args[0]["payload"]["strategyId"]
        for call in em.broadcast.await_args_list
    ]
    assert seen_ids == ["strategy1", "strategy2", "strategy3"]


@pytest.mark.asyncio
async def test_broadcast_strategy_update_payload_validates_against_pydantic_models() -> None:
    """Round-trip the emitted payload through the bot's StrategyUpdate +
    WsStrategyUpdate schemas to catch shape drift between
    utils.strategy_updates.broadcast_strategy_update and
    api.schemas.StrategyUpdate / WsStrategyUpdate. Mirrors the
    FILL-batch precedent."""
    from api.schemas import StrategyUpdate, WsStrategyUpdate

    em = _make_mock_emitter()
    set_emitter(em)

    broadcast_strategy_update(
        strategy_id="strategy1",
        updates={
            "enabled": True,
            "maxPositionSize": 100.0,
            "riskBudget": 0.33,
            "activePairs": "BTC-USD,ETH-USD",
        },
    )
    await asyncio.sleep(0)

    msg = em.broadcast.await_args.args[0]
    # If WsStrategyUpdate or StrategyUpdate fields drift, this raises.
    WsStrategyUpdate.model_validate(msg)
    StrategyUpdate.model_validate(msg["payload"])
