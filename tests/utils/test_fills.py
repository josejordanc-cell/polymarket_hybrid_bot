"""
tests/utils/test_fills.py — D.2.4 step 2 FILL broadcast wire-up tests.

Verifies that utils.fills.broadcast_fill broadcasts a flat
FillRecord-shaped payload through the registered Emitter, per the
contract reconciled with the dashboard zod schema in this batch.

See docs/audit/d24-emit-site-audit.md §5.1 (gate) and §10 (FILL drift
discovery + Level-1 pnl-nullable resolution) for context.

Structure mirrors tests/utils/test_alerts.py from the ALERT batch.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from api.emit import Emitter
from utils import fills as fills_mod
from utils.fills import broadcast_fill, set_emitter


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
async def test_broadcast_fill_broadcasts_when_emitter_registered() -> None:
    em = _make_mock_emitter()
    set_emitter(em)

    await broadcast_fill(
        market_id="0xABCDEF123456789",
        action="UP",
        price=0.42,
        size=10.0,
        strategy="strategy1",
        pnl=None,
    )

    assert em.broadcast.await_count == 1
    msg = em.broadcast.await_args.args[0]
    assert msg["type"] == "FILL"
    payload = msg["payload"]
    expected_keys = {
        "id", "time", "symbol", "action", "price",
        "size", "pnl", "status", "strategy",
    }
    assert set(payload.keys()) == expected_keys
    # symbol is truncated market_id (display-only mapping)
    assert payload["symbol"] == "0xABCDEF1234"
    assert payload["action"] == "UP"
    assert payload["price"] == 0.42
    assert payload["size"] == 10.0
    assert payload["pnl"] is None
    assert payload["status"] == "filled"
    assert payload["strategy"] == "strategy1"
    assert isinstance(payload["id"], str) and payload["id"]
    assert isinstance(payload["time"], str) and payload["time"]


@pytest.mark.asyncio
async def test_broadcast_fill_is_noop_when_no_emitter() -> None:
    # No set_emitter() call; _reset_emitter fixture ensures it is None.
    assert fills_mod._emitter is None
    # Must not raise.
    await broadcast_fill(
        market_id="m", action="DOWN", price=0.1, size=1.0, strategy="strategy2",
    )


@pytest.mark.asyncio
async def test_broadcast_fill_pnl_round_trip_when_provided() -> None:
    """When pnl is provided explicitly (e.g. future use), it round-trips
    as a float in the payload."""
    em = _make_mock_emitter()
    set_emitter(em)

    await broadcast_fill(
        market_id="m", action="UP", price=0.5, size=2.0,
        strategy="strategy1", pnl=12.34,
    )

    payload = em.broadcast.await_args.args[0]["payload"]
    assert payload["pnl"] == 12.34
    assert isinstance(payload["pnl"], float)


@pytest.mark.asyncio
async def test_broadcast_fill_mints_unique_ids_across_calls() -> None:
    em = _make_mock_emitter()
    set_emitter(em)

    await broadcast_fill(market_id="m1", action="UP", price=0.1, size=1.0, strategy="strategy1")
    await broadcast_fill(market_id="m2", action="DOWN", price=0.2, size=2.0, strategy="strategy2")

    ids = [call.args[0]["payload"]["id"] for call in em.broadcast.await_args_list]
    assert len(ids) == 2
    assert ids[0] != ids[1]
    assert all(isinstance(i, str) and i for i in ids)


@pytest.mark.asyncio
async def test_broadcast_fill_payload_validates_against_pydantic_fillrecord() -> None:
    """Round-trip the emitted payload through the bot's FillRecord schema to
    catch shape drift between utils.fills.broadcast_fill and
    api.schemas.FillRecord. Mirrors the ALERT-batch precedent."""
    from api.schemas import FillRecord

    em = _make_mock_emitter()
    set_emitter(em)

    await broadcast_fill(
        market_id="0xabc123def456",
        action="UP",
        price=0.6,
        size=5.0,
        strategy="strategy3",
        pnl=None,
    )

    payload = em.broadcast.await_args.args[0]["payload"]
    # If FillRecord fields drift, this raises ValidationError.
    FillRecord.model_validate(payload)
