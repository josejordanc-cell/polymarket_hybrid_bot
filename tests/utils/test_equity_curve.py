"""tests/utils/test_equity_curve.py — v0.2.9 B.7 wiring tests.

Covers utils/equity_curve.py:
  - _maybe_append_today: bootstrap idempotence, date-rollover
    detection, defensive swallow-on-error
  - equity_snapshot_loop: bootstrap append on start, periodic ticks,
    cancellation handling, defensive iteration-error recovery
  - BotState.equity_history deque(maxlen=200) bounded behavior
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from api.schemas import EquityPoint
from api.state import BotState
from utils.equity_curve import (
    _DEFAULT_INTERVAL_SEC,
    _maybe_append_today,
    equity_snapshot_loop,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_bot_state(equity: float = 10000.0, day_start: float = 9500.0) -> BotState:
    """Minimal BotState for equity-curve tests."""
    return BotState.create(equity=equity, day_start_equity=day_start)


# ---------------------------------------------------------------------------
# TestMaybeAppendToday — date-detection + idempotence
# ---------------------------------------------------------------------------


class TestMaybeAppendToday:
    @pytest.mark.asyncio
    async def test_appends_when_history_empty(self) -> None:
        """First call: nothing in equity_history → append today's point."""
        state = _make_bot_state(equity=10000.0, day_start=9500.0)
        assert len(state.equity_history) == 0

        await _maybe_append_today(state)

        assert len(state.equity_history) == 1
        point = state.equity_history[0]
        assert isinstance(point, EquityPoint)
        # day matches UTC today.
        assert point.day == datetime.now(timezone.utc).date().isoformat()
        assert point.equity == 10000.0
        assert point.pnl == 500.0  # 10000 - 9500

    @pytest.mark.asyncio
    async def test_idempotent_within_same_day(self) -> None:
        """Calling twice on the same UTC day → second call is a no-op."""
        state = _make_bot_state()
        await _maybe_append_today(state)
        first_count = len(state.equity_history)

        # Second call — same day → no append.
        await _maybe_append_today(state)
        assert len(state.equity_history) == first_count

    @pytest.mark.asyncio
    async def test_appends_when_last_day_is_yesterday(self) -> None:
        """UTC-day rollover: if the most recent point's day != today,
        a new point is appended (the production rollover path)."""
        state = _make_bot_state(equity=11000.0, day_start=10000.0)
        # Seed with a "yesterday" point.
        state.equity_history.append(
            EquityPoint(day="2020-01-01", equity=9000.0, pnl=0.0)
        )

        await _maybe_append_today(state)

        assert len(state.equity_history) == 2
        new_point = state.equity_history[-1]
        assert new_point.day == datetime.now(timezone.utc).date().isoformat()
        assert new_point.equity == 11000.0
        assert new_point.pnl == 1000.0  # 11000 - 10000

    @pytest.mark.asyncio
    async def test_pnl_is_equity_minus_day_start(self) -> None:
        """PnL semantics: state.equity - state.day_start_equity.
        Matches globalStats.pnlToday."""
        state = _make_bot_state(equity=10500.0, day_start=10000.0)
        await _maybe_append_today(state)
        assert state.equity_history[0].pnl == 500.0

    @pytest.mark.asyncio
    async def test_negative_pnl_supported(self) -> None:
        """Loss days: equity < day_start_equity → negative pnl."""
        state = _make_bot_state(equity=9500.0, day_start=10000.0)
        await _maybe_append_today(state)
        assert state.equity_history[0].pnl == -500.0

    @pytest.mark.asyncio
    async def test_defensive_swallow_on_error(self) -> None:
        """v0.2.9 B.7 contract: _maybe_append_today never raises. If
        the deque write or any internal step throws, the exception is
        logged and swallowed so the parent loop continues."""
        state = _make_bot_state()

        # Patch utc_now to raise — simulates a clock subsystem failure.
        with patch(
            "utils.equity_curve.utc_now",
            side_effect=RuntimeError("clock failure"),
        ):
            # Must NOT raise. The caller (the loop) must keep ticking.
            await _maybe_append_today(state)

        # No point was appended.
        assert len(state.equity_history) == 0


# ---------------------------------------------------------------------------
# TestEquitySnapshotLoop — bootstrap + periodic ticks + cancellation
# ---------------------------------------------------------------------------


class TestEquitySnapshotLoop:
    @pytest.mark.asyncio
    async def test_bootstrap_appends_immediately(self) -> None:
        """v0.2.9 B.7 contract: loop start triggers a bootstrap
        _maybe_append_today BEFORE the first asyncio.sleep. Tests this
        by starting the loop, yielding once, and cancelling — the
        bootstrap append must have landed."""
        state = _make_bot_state()

        task = asyncio.create_task(equity_snapshot_loop(state))
        # Yield once for the bootstrap to fire. asyncio.sleep(0) lets
        # the loop run its bootstrap before hitting the sleep().
        await asyncio.sleep(0)
        await asyncio.sleep(0)  # double-yield for nested awaits

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert len(state.equity_history) == 1

    @pytest.mark.asyncio
    async def test_periodic_ticks_via_mocked_sleep(self) -> None:
        """Drive 3 iterations by mocking asyncio.sleep. Each iteration
        calls _maybe_append_today. Since each call lands on the same
        UTC day, only the FIRST (bootstrap) appends — the rest are
        no-ops by the idempotence contract. The TEST is: the loop
        body fires 3 times without erroring."""
        state = _make_bot_state()
        call_count = 0

        async def fake_sleep(secs):
            nonlocal call_count
            call_count += 1
            if call_count >= 3:
                # Cancel the task on the 3rd sleep to end the loop.
                raise asyncio.CancelledError()

        with patch("utils.equity_curve.asyncio.sleep", side_effect=fake_sleep):
            try:
                await equity_snapshot_loop(state, interval_sec=1)
            except asyncio.CancelledError:
                pass

        # Bootstrap fired (1 point); subsequent ticks on same day were
        # no-ops by design.
        assert len(state.equity_history) == 1
        # The fake sleep was called 3 times (3 loop iterations).
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_cancellation_propagates_cleanly(self) -> None:
        """The loop must propagate asyncio.CancelledError for clean
        shutdown. ``api/server.py``'s finally block depends on this."""
        state = _make_bot_state()

        task = asyncio.create_task(
            equity_snapshot_loop(state, interval_sec=3600)
        )
        # Yield for bootstrap.
        await asyncio.sleep(0)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    @pytest.mark.asyncio
    async def test_iteration_error_does_not_kill_loop(self) -> None:
        """Defensive contract: any unforeseen exception inside the
        loop body is logged and swallowed; the loop keeps ticking.
        Tests by making asyncio.sleep raise a non-CancelledError on
        the first call and a CancelledError on the second."""
        state = _make_bot_state()
        sleep_calls = 0

        async def flaky_sleep(secs):
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls == 1:
                raise RuntimeError("sleep flaked")
            raise asyncio.CancelledError()

        with patch("utils.equity_curve.asyncio.sleep", side_effect=flaky_sleep):
            try:
                await equity_snapshot_loop(state, interval_sec=1)
            except asyncio.CancelledError:
                pass

        # The RuntimeError was swallowed; the loop did at least 2
        # sleep iterations (the flaky one + the cancel one).
        assert sleep_calls == 2
        # Bootstrap append landed.
        assert len(state.equity_history) == 1


# ---------------------------------------------------------------------------
# TestEquityHistoryBounded — deque(maxlen=200) wiring
# ---------------------------------------------------------------------------


class TestEquityHistoryBounded:
    """B.7 promoted BotState.equity_history from a plain list to
    deque(maxlen=200). Mirror the recent_alerts/recent_logs bounded-
    behavior tests."""

    def test_default_factory_is_deque_with_maxlen_200(self) -> None:
        state = _make_bot_state()
        from collections import deque

        assert isinstance(state.equity_history, deque)
        assert state.equity_history.maxlen == 200

    def test_append_beyond_maxlen_drops_oldest(self) -> None:
        """maxlen=200 → 201st append drops the oldest point."""
        state = _make_bot_state()
        maxlen = state.equity_history.maxlen
        assert maxlen == 200

        for i in range(maxlen + 1):
            state.equity_history.append(
                EquityPoint(day=f"2020-{(i % 12) + 1:02d}-01", equity=float(i), pnl=0.0)
            )

        assert len(state.equity_history) == maxlen
        # The 0th entry (equity=0.0) was dropped; equity=1.0 is now first.
        assert state.equity_history[0].equity == 1.0
        # The 200th entry (equity=200.0) is the newest.
        assert state.equity_history[-1].equity == float(maxlen)

    def test_shallow_copy_preserves_maxlen(self) -> None:
        """BotState.shallow_copy must preserve the maxlen on the copy
        (mirrors recent_logs/recent_alerts copy behavior)."""
        state = _make_bot_state()
        copy = state.shallow_copy()
        assert copy.equity_history.maxlen == 200
        # Copy is independent from the original.
        assert copy.equity_history is not state.equity_history
