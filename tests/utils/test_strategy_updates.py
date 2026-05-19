"""
tests/utils/test_strategy_updates.py — broadcast infrastructure tests
for STRATEGY_UPDATE WS messages.

Verifies that utils.strategy_updates.broadcast_strategy_update
broadcasts a flat StrategyUpdate-shaped payload through the registered
Emitter, with payload shape conforming to BOT_CONTRACT.md:413
(updates = partial of StrategyCardData).

NOTE (v0.2.8 post-tag correction): the module under test has no
currently-wired caller. An earlier version was called from
api/state.py:apply_strategy_patch with StrategyConfigPatch fields
(d1d2276), which violated the contract; that wiring was reverted.
These tests exercise the broadcast infrastructure directly so it
remains green and ready for the correct v0.2.9 caller (the
strategy-telemetry pipeline). See docs/audit/d24-emit-site-audit.md
§6.0 (three strategy surfaces) and §10 (correction block).

Structure: broadcast_strategy_update is SYNC and schedules em.broadcast()
as a task on the running event loop. Tests are async to provide a
running loop; the broadcast call site is sync but yields control back
via ``await asyncio.sleep(0)`` for the scheduled task to run.
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
        updates={"status": "paused", "pnlToday": -50.0},
    )

    # Sync function schedules an async task; let it run.
    await asyncio.sleep(0)

    assert em.broadcast.await_count == 1
    msg = em.broadcast.await_args.args[0]
    assert msg["type"] == "STRATEGY_UPDATE"
    payload = msg["payload"]
    assert set(payload.keys()) == {"strategyId", "updates"}
    assert payload["strategyId"] == "strategy1"
    assert payload["updates"] == {"status": "paused", "pnlToday": -50.0}


@pytest.mark.asyncio
async def test_broadcast_strategy_update_is_noop_when_no_emitter() -> None:
    # No set_emitter() call; _reset_emitter fixture ensures it is None.
    assert strategy_updates_mod._emitter is None
    # Must not raise.
    broadcast_strategy_update(strategy_id="strategy2", updates={"status": "live"})
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
    broadcast_strategy_update(strategy_id="strategy1", updates={"status": "paused"})

    # broadcast should never have been called.
    assert em.broadcast.await_count == 0


@pytest.mark.asyncio
async def test_broadcast_strategy_update_handles_multiple_calls() -> None:
    em = _make_mock_emitter()
    set_emitter(em)

    broadcast_strategy_update(strategy_id="strategy1", updates={"status": "live"})
    broadcast_strategy_update(strategy_id="strategy2", updates={"pnlToday": 100.0})
    broadcast_strategy_update(strategy_id="strategy3", updates={"tradesToday": 42})

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
            "status": "paused",
            "pnlToday": -50.0,
            "tradesToday": 0,
            "lastSignal": "EXIT",
        },
    )
    await asyncio.sleep(0)

    msg = em.broadcast.await_args.args[0]
    # If WsStrategyUpdate or StrategyUpdate fields drift, this raises.
    # Per BOT_CONTRACT.md:413, the payload is partial-of-StrategyCardData.
    WsStrategyUpdate.model_validate(msg)
    StrategyUpdate.model_validate(msg["payload"])
    # Verify the parsed updates match exactly what we sent (no field
    # silently dropped). Catches the self-consistent-but-wrong case
    # the d1d2276 wiring fell into.
    parsed_update = StrategyUpdate.model_validate(msg["payload"])
    assert parsed_update.updates.status == "paused"
    assert parsed_update.updates.pnlToday == -50.0
    assert parsed_update.updates.tradesToday == 0
    assert parsed_update.updates.lastSignal == "EXIT"
