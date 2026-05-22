"""tests/feeds/test_spot_feed_tick_emit.py

Coverage for the v0.2.9 3i TICK WS emit wire in
``data_feeds/spot_feed.py``: the ``_build_price_tick`` helper, the
per-stream 250 ms throttle, and the fire-and-forget broadcast via the
global Emitter (mocked).

Pure unit tests — no ccxt, no real WS connection, no real BotConfig.
``_record`` is called directly on a constructed ``SpotFeed`` with
the global emitter patched to an ``AsyncMock``.

Renamed from ``test_binance_feed_tick_emit.py`` in Round 9 alongside
the ``BinanceFeed`` → ``SpotFeed`` class / module rename; see
``.swarm-decisions.md`` entry ``v029-spotfeed-rename``.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.schemas import PriceTick
from data_feeds.spot_feed import (
    _TICK_EMIT_INTERVAL_SEC,
    SpotFeed,
    _build_price_tick,
)


# ---------------------------------------------------------------------------
# _build_price_tick — pure helper
# ---------------------------------------------------------------------------


def test_build_price_tick_canonical_5_field_shape() -> None:
    """v0.2.9 3i policy lock 6: PriceTick wire fields match Zod —
    symbol, last, bid, ask, ts. bid/ask default to None; ts is epoch ms."""
    ts = datetime(2024, 5, 21, 12, 0, 0, tzinfo=timezone.utc)
    tick = _build_price_tick("BTC/USDT", 77800.0, ts)
    assert isinstance(tick, PriceTick)
    data = tick.model_dump(mode="json")
    assert list(data.keys()) == ["symbol", "last", "bid", "ask", "ts"]
    assert data["symbol"] == "BTC/USDT"
    assert data["last"] == 77800.0
    assert data["bid"] is None
    assert data["ask"] is None
    # ts is 13-digit epoch ms.
    assert data["ts"] == int(ts.timestamp() * 1000)
    assert 1_000_000_000_000 <= data["ts"] < 100_000_000_000_000


def test_build_price_tick_ts_is_int_not_float() -> None:
    """v0.2.9 3i policy lock 5: ts MUST be int (13-digit epoch ms),
    not float seconds. Documents the unit explicitly."""
    ts = datetime(2024, 5, 21, 12, 0, 0, tzinfo=timezone.utc)
    tick = _build_price_tick("ETH/USDT", 3400.0, ts)
    assert isinstance(tick.ts, int)
    assert tick.ts > 1_000_000_000_000  # rejects pre-3i SEC form (~1.7e9)


def test_build_price_tick_symbol_is_verbatim() -> None:
    """v0.2.9 3i policy lock 1: symbol ships BASE/QUOTE verbatim — no
    normalization, no venue prefix, no quote collapse."""
    ts = datetime(2024, 5, 21, 12, 0, 0, tzinfo=timezone.utc)
    assert _build_price_tick("BTC/USDT", 1.0, ts).symbol == "BTC/USDT"
    assert _build_price_tick("BTC/USD", 1.0, ts).symbol == "BTC/USD"


# ---------------------------------------------------------------------------
# Test fixtures for SpotFeed instance
# ---------------------------------------------------------------------------


def _make_feed() -> SpotFeed:
    """Construct a SpotFeed with a minimal mock config. We don't
    drive the ccxt run loop in any of these tests — just _record / the
    throttle / the emit path."""
    config = MagicMock()
    config.spot.binance_symbols = ["BTC/USDT", "ETH/USDT"]
    config.spot.coinbase_symbols = ["BTC/USD", "ETH/USD"]
    return SpotFeed(config)


# ---------------------------------------------------------------------------
# Throttle + emit tests — patch get_emitter to a stub
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_emits_once_when_emitter_registered() -> None:
    """First tick on a stream produces exactly one broadcast call."""
    feed = _make_feed()
    em = AsyncMock()
    em.broadcast = AsyncMock()
    with patch("data_feeds.spot_feed.get_emitter", return_value=em):
        await feed._record("binance", "BTC/USDT", 77800.0)
        # asyncio.create_task schedules the broadcast on the running
        # loop; yield once so the task actually executes.
        await asyncio.sleep(0)

    em.broadcast.assert_awaited_once()
    call_payload = em.broadcast.await_args.args[0]
    assert call_payload["type"] == "TICK"
    assert call_payload["payload"]["symbol"] == "BTC/USDT"
    assert call_payload["payload"]["last"] == 77800.0
    assert call_payload["payload"]["bid"] is None
    assert call_payload["payload"]["ask"] is None
    assert isinstance(call_payload["payload"]["ts"], int)


@pytest.mark.asyncio
async def test_record_no_emitter_silently_skips() -> None:
    """Headless / test envs without a registered Emitter must not
    raise — broadcast_fill precedent applies. _record completes
    cleanly and stores the tick locally."""
    feed = _make_feed()
    with patch("data_feeds.spot_feed.get_emitter", return_value=None):
        await feed._record("binance", "BTC/USDT", 77800.0)
        await asyncio.sleep(0)

    # Storage path still happened.
    assert feed._last_ticks["binance"]["BTC/USDT"].price == 77800.0


@pytest.mark.asyncio
async def test_throttle_suppresses_second_emit_within_250ms() -> None:
    """v0.2.9 3i policy lock 3: per-stream cap of one frame per 250 ms.
    Two ticks issued back-to-back (microseconds apart, well under
    250 ms) on the same (venue, symbol) → one broadcast."""
    feed = _make_feed()
    em = AsyncMock()
    em.broadcast = AsyncMock()
    with patch("data_feeds.spot_feed.get_emitter", return_value=em):
        await feed._record("binance", "BTC/USDT", 77800.0)
        await asyncio.sleep(0)
        # No real sleep between calls — elapsed ≪ 250 ms, throttle
        # MUST suppress.
        await feed._record("binance", "BTC/USDT", 77810.0)
        await asyncio.sleep(0)

    assert em.broadcast.await_count == 1


@pytest.mark.asyncio
async def test_throttle_allows_second_emit_after_250ms() -> None:
    """Two ticks > 250 ms apart on the same stream → two broadcasts.
    Drives the elapsed comparison by back-dating ``feed._last_emit``
    rather than patching ``time.monotonic`` (the latter would also
    perturb asyncio's event-loop clock and break task scheduling)."""
    feed = _make_feed()
    em = AsyncMock()
    em.broadcast = AsyncMock()
    with patch("data_feeds.spot_feed.get_emitter", return_value=em):
        await feed._record("binance", "BTC/USDT", 77800.0)
        await asyncio.sleep(0)
        assert em.broadcast.await_count == 1

        # Backdate the throttle entry so the next record's elapsed
        # exceeds the 250 ms threshold.
        feed._last_emit[("binance", "BTC/USDT")] = time.monotonic() - 1.0

        await feed._record("binance", "BTC/USDT", 77810.0)
        await asyncio.sleep(0)

    assert em.broadcast.await_count == 2


@pytest.mark.asyncio
async def test_throttle_is_per_stream_not_global() -> None:
    """v0.2.9 3i: throttle keyed by (venue, symbol). A tick on
    (binance, BTC/USDT) must NOT reset the throttle for
    (coinbase, BTC/USD) — these are independent streams. Both fire
    even when called back-to-back."""
    feed = _make_feed()
    em = AsyncMock()
    em.broadcast = AsyncMock()
    with patch("data_feeds.spot_feed.get_emitter", return_value=em):
        await feed._record("binance", "BTC/USDT", 77800.0)
        await asyncio.sleep(0)
        # Immediate second tick on a DIFFERENT stream — would be
        # suppressed if the throttle were global; per-stream → emits.
        await feed._record("coinbase", "BTC/USD", 77810.0)
        await asyncio.sleep(0)

    assert em.broadcast.await_count == 2
    # Verify the two payloads carry distinct symbols.
    payloads = [
        call.args[0]["payload"] for call in em.broadcast.await_args_list
    ]
    symbols = {p["symbol"] for p in payloads}
    assert symbols == {"BTC/USDT", "BTC/USD"}


@pytest.mark.asyncio
async def test_throttle_is_per_symbol_within_same_venue() -> None:
    """Two different symbols on the same venue are still distinct
    streams — both emit even when called back-to-back."""
    feed = _make_feed()
    em = AsyncMock()
    em.broadcast = AsyncMock()
    with patch("data_feeds.spot_feed.get_emitter", return_value=em):
        await feed._record("binance", "BTC/USDT", 77800.0)
        await asyncio.sleep(0)
        await feed._record("binance", "ETH/USDT", 3400.0)
        await asyncio.sleep(0)

    assert em.broadcast.await_count == 2


@pytest.mark.asyncio
async def test_emit_payload_matches_pricetick_model_dump() -> None:
    """v0.2.9 3i: the wire payload is exactly PriceTick.model_dump
    (mode='json'). Regression guard against accidental key reshuffling
    or unit-of-time drift between the helper and the model."""
    feed = _make_feed()
    em = AsyncMock()
    em.broadcast = AsyncMock()
    with patch("data_feeds.spot_feed.get_emitter", return_value=em):
        await feed._record("binance", "BTC/USDT", 77800.0)
        await asyncio.sleep(0)

    payload = em.broadcast.await_args.args[0]["payload"]
    # Reconstruct a PriceTick from the payload and verify round-trip
    # consistency.
    tick = PriceTick.model_validate(payload)
    assert tick.symbol == "BTC/USDT"
    assert tick.last == 77800.0
    assert tick.bid is None
    assert tick.ask is None
    assert isinstance(tick.ts, int)


@pytest.mark.asyncio
async def test_constant_throttle_interval_is_250ms() -> None:
    """v0.2.9 3i policy lock 3: 250 ms exact. Document the constant
    here so any future change to the literal triggers a test failure
    and forces a policy-decision review."""
    assert _TICK_EMIT_INTERVAL_SEC == 0.25
