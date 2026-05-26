"""tests/test_risk_state_mirror.py

Unit tests for utils/risk_state_mirror.py (profitability epic piece (2b)).
Targets the helper directly with a stub RiskManager; the live wiring in
api/server.py:run() is helper-unit-covered but runtime-unverified until
the droplet sim smoke — recorded as an owed gap in .swarm-decisions
b1-fill-runtime-update (parallel deferral for the 2a wiring; 2b has a
lighter close criterion since the loop ticks unconditionally every 5s).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from api.state import BotState
import utils.risk_state_mirror as mirror_mod
from utils.risk_state_mirror import _mirror_once, mirror_risk_to_botstate


# ---------------------------------------------------------------------------
# Stub RiskManager — exposes only the three public accessors the mirror reads
# ---------------------------------------------------------------------------


@dataclass
class _StubDailyPnL:
    start_equity_usd: float = 1000.0


@dataclass
class _StubRiskManager:
    """Minimal stub exposing exactly the three public accessors the (2b)
    mirror is allowed to read: ``equity`` (property), ``open_positions``
    (property returning a defensive copy), ``daily_pnl_snapshot()`` (method).

    Mutators ``set_*`` let the test drive the stub between mirror cycles.
    """
    _equity: float = 1000.0
    _open_positions: dict[str, Any] = field(default_factory=dict)
    _day_start_equity: float = 1000.0
    daily_pnl_snapshot_call_count: int = 0

    @property
    def equity(self) -> float:
        return self._equity

    @property
    def open_positions(self) -> dict[str, Any]:
        # Match the real RiskManager.open_positions defensive-copy behaviour
        return dict(self._open_positions)

    def daily_pnl_snapshot(self) -> _StubDailyPnL:
        self.daily_pnl_snapshot_call_count += 1
        return _StubDailyPnL(start_equity_usd=self._day_start_equity)

    def set_equity(self, value: float) -> None:
        self._equity = value

    def set_open_positions(self, positions: dict[str, Any]) -> None:
        self._open_positions = positions

    def set_day_start_equity(self, value: float) -> None:
        self._day_start_equity = value


def _fresh_state(equity: float = 0.0, day_start_equity: float = 0.0) -> BotState:
    return BotState.create(
        equity=equity,
        day_start_equity=day_start_equity,
        open_positions={},
    )


# ---------------------------------------------------------------------------
# _mirror_once — single-cycle behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_equity_mirrored_correctly() -> None:
    risk = _StubRiskManager(_equity=12345.67)
    state = _fresh_state()
    await _mirror_once(risk, state)
    assert state.equity == 12345.67


@pytest.mark.asyncio
async def test_day_start_equity_pulled_from_daily_pnl_snapshot() -> None:
    """state.day_start_equity must come from risk.daily_pnl_snapshot()
    .start_equity_usd — NOT from any other RiskManager attribute, and
    NOT from risk._state (private surface forbidden in this scope)."""
    risk = _StubRiskManager(_day_start_equity=9876.54)
    state = _fresh_state()
    await _mirror_once(risk, state)
    assert state.day_start_equity == 9876.54
    # daily_pnl_snapshot() must have been called exactly once per cycle
    assert risk.daily_pnl_snapshot_call_count == 1


@pytest.mark.asyncio
async def test_open_positions_atomic_replace_not_merge() -> None:
    """state.open_positions must be REPLACED, not merged. If risk has
    only positions {B, C} on the second cycle, state must NOT carry
    forward position A from the first cycle."""
    risk = _StubRiskManager(_open_positions={"A": "pos_a"})
    state = _fresh_state()

    # First cycle: A only
    await _mirror_once(risk, state)
    assert set(state.open_positions.keys()) == {"A"}

    # Second cycle: A gone, B and C present
    risk.set_open_positions({"B": "pos_b", "C": "pos_c"})
    await _mirror_once(risk, state)
    assert set(state.open_positions.keys()) == {"B", "C"}
    assert "A" not in state.open_positions  # not merged forward


@pytest.mark.asyncio
async def test_open_positions_is_fresh_dict_not_shared_reference() -> None:
    """state.open_positions must be a distinct dict from risk.open_positions
    so subsequent mutations on either side don't leak across the boundary."""
    initial_positions = {"A": "pos_a"}
    risk = _StubRiskManager(_open_positions=initial_positions)
    state = _fresh_state()
    await _mirror_once(risk, state)
    # Risk's property already returns a defensive copy; the mirror does dict()
    # over that, so state.open_positions is doubly-detached from risk._open_positions.
    assert state.open_positions is not initial_positions


@pytest.mark.asyncio
async def test_write_happens_under_write_lock() -> None:
    """The three writes must happen atomically under state.write_lock so
    a concurrent snapshot reader cannot observe a half-mutated state."""
    risk = _StubRiskManager(_equity=500.0, _day_start_equity=1000.0)
    state = _fresh_state()

    # Acquire the lock externally before launching the mirror — the mirror's
    # `async with state.write_lock` should block until we release.
    lock_was_held_during_mirror = asyncio.Event()
    mirror_completed = asyncio.Event()

    async def _hold_then_release() -> None:
        async with state.write_lock:
            lock_was_held_during_mirror.set()
            # Launch the mirror while we still hold the lock
            mirror_task = asyncio.create_task(_mirror_once(risk, state))
            # Give the mirror a chance to be scheduled and block on the lock
            await asyncio.sleep(0.05)
            # Mirror must NOT have completed yet — it's waiting on the lock
            assert not mirror_task.done()
            # State must still be at its pre-mirror values
            assert state.equity == 0.0
        # Lock released — mirror should now proceed
        await asyncio.wait_for(mirror_task, timeout=1.0)
        mirror_completed.set()

    await _hold_then_release()
    assert lock_was_held_during_mirror.is_set()
    assert mirror_completed.is_set()
    # Post-mirror values landed
    assert state.equity == 500.0
    assert state.day_start_equity == 1000.0


# ---------------------------------------------------------------------------
# mirror_risk_to_botstate — loop behaviour (poll-first + cancellation)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poll_first_ordering_write_before_first_sleep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first write must land BEFORE the first asyncio.sleep — so a
    freshly-restarted bot serves live values from the next snapshot
    request, not after a 5-second blank window."""
    risk = _StubRiskManager(_equity=42.0)
    state = _fresh_state()

    sleep_calls: list[float] = []
    state_at_first_sleep: dict[str, Any] = {}

    real_sleep = asyncio.sleep

    async def _tracking_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        # Capture state at the moment of the first sleep
        if len(sleep_calls) == 1:
            state_at_first_sleep["equity"] = state.equity
        # Cancel the loop after a brief real sleep so the test terminates
        await real_sleep(0.01)
        raise asyncio.CancelledError

    monkeypatch.setattr(mirror_mod.asyncio, "sleep", _tracking_sleep)

    with pytest.raises(asyncio.CancelledError):
        await mirror_risk_to_botstate(risk, state, interval_sec=5)

    # The first sleep happened AFTER the first write
    assert state_at_first_sleep.get("equity") == 42.0
    # And the interval passed through correctly
    assert sleep_calls[0] == 5


@pytest.mark.asyncio
async def test_cancellation_handled_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The loop must exit cleanly on CancelledError without raising
    anything other than CancelledError (matches equity_curve pattern)."""
    risk = _StubRiskManager()
    state = _fresh_state()

    task = asyncio.create_task(
        mirror_risk_to_botstate(risk, state, interval_sec=60)
    )
    # Let the poll-first write complete + enter the sleep
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # Task is done, no leftover exception other than the cancellation
    assert task.cancelled()


@pytest.mark.asyncio
async def test_multiple_cycles_track_risk_state_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Over multiple cycles, state should track changing risk values."""
    risk = _StubRiskManager(_equity=100.0)
    state = _fresh_state()

    observed: list[float] = []
    cycle_count = {"n": 0}

    real_sleep = asyncio.sleep

    async def _short_sleep(seconds: float) -> None:
        observed.append(state.equity)
        cycle_count["n"] += 1
        if cycle_count["n"] >= 3:
            raise asyncio.CancelledError
        # Bump risk between cycles
        risk.set_equity(risk.equity + 50.0)
        await real_sleep(0.001)

    monkeypatch.setattr(mirror_mod.asyncio, "sleep", _short_sleep)

    with pytest.raises(asyncio.CancelledError):
        await mirror_risk_to_botstate(risk, state, interval_sec=5)

    # First cycle (poll-first): state.equity == 100, observed at first sleep == 100
    # Between cycle 1 and 2, risk bumped to 150
    # Second cycle: state.equity == 150, observed at second sleep == 150
    # Between cycle 2 and 3, risk bumped to 200
    # Third cycle: state.equity == 200, observed at third sleep == 200 → cancel
    assert observed == [100.0, 150.0, 200.0]
