"""tests/test_resolution_polling_loop.py

Integration test for main.resolution_polling_loop with the PR2 settlement
persistence wiring. Unlike _exec_and_record (nested inside the strategy
loop, untestable without refactor — see b1-fill-runtime-deferred), the
resolution_polling_loop is a module-level async coroutine and can be
driven directly with mocked dependencies.

This test asserts end-to-end:
  1. poll_market_resolutions is called with the open-position market_ids
  2. For each resolving market, risk.on_settlement is invoked
  3. The caller computes pnl = realised_pnl delta via daily_pnl_snapshot()
  4. persist_settlement appends a row to BOT_DATA_DIR/settlements.jsonl
  5. compute_win_rate and compute_streak see that row immediately
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import data_feeds.polymarket_feed as polymarket_feed_mod
from main import resolution_polling_loop
from utils.derived_stats import compute_streak, compute_win_rate


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


@dataclass
class _StubPosition:
    market_id: str
    outcome: str
    quantity: float
    avg_price: float


@dataclass
class _StubResolutionEvent:
    """Matches data_feeds.polymarket_feed.ResolutionEvent's surface
    (market_id: str, resolved_outcome: Literal['YES','NO'])."""
    market_id: str
    resolved_outcome: str


class _StubRiskManager:
    """Faithful mirror of the public surface resolution_polling_loop
    touches, modelling on_settlement EXACTLY as risk_manager.py:322-332:
    ``equity += proceeds`` (proceeds only) and
    ``realised_pnl += proceeds - notional`` (the true round-trip P&L).
    Exposes the ``equity`` / ``open_positions`` properties and
    ``daily_pnl_snapshot()`` — the public realised-P&L accessor the fixed
    loop reads. Modelling equity and realised_pnl as DISTINCT quantities
    is the whole point: a loss moves realised_pnl negative while leaving
    equity flat (proceeds=0), which the old equity-delta capture mistook
    for break-even."""

    def __init__(
        self,
        equity: float,
        positions: dict[str, _StubPosition],
        realised_pnl: float = 0.0,
    ) -> None:
        self._equity = equity
        self._open_positions = dict(positions)
        self._realised_pnl = realised_pnl
        self.on_settlement_calls: list[tuple[str, str]] = []

    @property
    def equity(self) -> float:
        return self._equity

    @property
    def open_positions(self) -> dict[str, _StubPosition]:
        return dict(self._open_positions)

    def daily_pnl_snapshot(self) -> SimpleNamespace:
        """Public accessor mirrored from RiskManager.daily_pnl_snapshot();
        resolution_polling_loop reads only ``realised_pnl_usd``."""
        return SimpleNamespace(realised_pnl_usd=self._realised_pnl)

    def on_settlement(self, market_id: str, winning_outcome: str) -> None:
        """Mirror risk_manager.py:322-332: for each position on this
        market, ``equity += proceeds`` and ``realised_pnl += proceeds -
        notional``, then pop the position. proceeds = quantity if the
        position's own outcome won, else 0 (sync, void return)."""
        self.on_settlement_calls.append((market_id, winning_outcome))
        for key in list(self._open_positions.keys()):
            pos = self._open_positions[key]
            if pos.market_id != market_id:
                continue
            proceeds = pos.quantity * 1.0 if pos.outcome == winning_outcome else 0.0
            pnl = proceeds - pos.quantity * pos.avg_price
            self._equity += proceeds
            self._realised_pnl += pnl
            del self._open_positions[key]


def _stub_config(poll_seconds: float = 0.01) -> SimpleNamespace:
    return SimpleNamespace(resolution_poll_seconds=poll_seconds)


# ---------------------------------------------------------------------------
# Driver helper
# ---------------------------------------------------------------------------


async def _drive_one_iteration(
    risk: _StubRiskManager,
    events: list[_StubResolutionEvent],
    monkeypatch: pytest.MonkeyPatch,
    timeout: float = 1.0,
) -> None:
    """Patch poll_market_resolutions to return `events`, then run
    resolution_polling_loop until shutdown is set after the first
    event-processing pass.

    The mock sets the shutdown event AFTER returning events, so the
    _interruptible_sleep at the end of the iteration trips immediately
    and the loop exits cleanly.
    """
    shutdown = asyncio.Event()

    async def _mock_poll(_market_ids: list[str]) -> list[_StubResolutionEvent]:
        # Trigger shutdown so the next _interruptible_sleep returns True
        # and the loop exits after processing these events.
        shutdown.set()
        return events

    monkeypatch.setattr(
        polymarket_feed_mod, "poll_market_resolutions", _mock_poll
    )

    config = _stub_config()
    await asyncio.wait_for(
        resolution_polling_loop(risk, config, shutdown),
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_winning_settlement_persists_and_compute_helpers_see_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: a winning settlement (+pnl) produces a settlements.jsonl
    row; compute_win_rate sees 1.0; compute_streak sees +1."""
    monkeypatch.setenv("BOT_DATA_DIR", str(tmp_path))

    # Long YES @ 0.5, qty 100; resolves YES → win.
    # realised_pnl delta = 100 - 100*0.5 = +50 (> 0).
    risk = _StubRiskManager(
        equity=1000.0,
        positions={
            "0xabc_YES": _StubPosition(
                market_id="0xabc", outcome="YES", quantity=100.0, avg_price=0.5
            )
        },
    )
    events = [_StubResolutionEvent(market_id="0xabc", resolved_outcome="YES")]

    await _drive_one_iteration(risk, events, monkeypatch)

    # Sanity: on_settlement actually called
    assert risk.on_settlement_calls == [("0xabc", "YES")]

    # settlements.jsonl was written
    out = tmp_path / "settlements.jsonl"
    assert out.is_file()
    rows = [
        line for line in out.read_text().splitlines() if line.strip()
    ]
    assert len(rows) == 1

    # Compute helpers see the new row immediately.
    # winRate is on a 0–100 scale (locked cross-repo contract; dashboard appends "%").
    assert compute_win_rate() == 100.0
    assert compute_streak() == 1


@pytest.mark.asyncio
async def test_losing_settlement_persists_with_negative_pnl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A losing settlement (-pnl) produces a row with winning=False;
    compute_win_rate sees 0.0; compute_streak sees -1."""
    import json

    monkeypatch.setenv("BOT_DATA_DIR", str(tmp_path))

    # Long NO @ 0.5, qty 50; market resolves YES → the NO leg loses.
    # realised_pnl delta = 0 - 50*0.5 = -25 (< 0). Equity is UNCHANGED
    # (proceeds=0) — the loss was already realised at fill time; the old
    # equity-delta capture wrongly persisted this as pnl=0 (break-even).
    risk = _StubRiskManager(
        equity=1000.0,
        positions={
            "0xabc_NO": _StubPosition(
                market_id="0xabc", outcome="NO", quantity=50.0, avg_price=0.5
            )
        },
    )
    events = [_StubResolutionEvent(market_id="0xabc", resolved_outcome="YES")]

    await _drive_one_iteration(risk, events, monkeypatch)

    out = tmp_path / "settlements.jsonl"
    rows = [json.loads(line) for line in out.read_text().splitlines() if line.strip()]
    assert len(rows) == 1
    row = rows[0]
    assert row["market_id"] == "0xabc"
    assert row["resolved_outcome"] == "YES"
    assert row["pnl"] == pytest.approx(-25.0)
    # equity += proceeds(0) → unchanged at 1000.0 (was 975.0; that
    # assertion pinned the old equity-delta bug, where the persisted pnl
    # WAS the equity delta. Post-fix the two are decoupled).
    assert row["equity_after"] == pytest.approx(1000.0)
    assert row["winning"] is False

    assert compute_win_rate() == 0.0
    assert compute_streak() == -1


@pytest.mark.asyncio
async def test_no_open_positions_skips_settlement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the bot has no open position matching the event's market_id,
    on_settlement must NOT be called and no row should be written. The
    filter at main.py:111 enforces this."""
    monkeypatch.setenv("BOT_DATA_DIR", str(tmp_path))

    risk = _StubRiskManager(
        equity=1000.0,
        positions={
            "0xother_YES": _StubPosition(
                market_id="0xother", outcome="YES", quantity=10.0, avg_price=0.5
            )
        },
    )
    events = [_StubResolutionEvent(market_id="0xabc", resolved_outcome="YES")]

    # NOTE: in this scenario, market_ids contains "0xother" only, so
    # poll_market_resolutions is called but the returned event's
    # market_id won't match any open position — main.py:111-112 skips it.
    await _drive_one_iteration(risk, events, monkeypatch)

    # on_settlement was NOT called for 0xabc
    assert risk.on_settlement_calls == []
    # No settlements.jsonl row written
    out = tmp_path / "settlements.jsonl"
    assert (not out.exists()) or out.read_text().strip() == ""


@pytest.mark.asyncio
async def test_empty_open_positions_skips_polling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When risk.open_positions is empty, the early-return at main.py:101
    skips the entire poll cycle. on_settlement isn't called and no rows
    are written."""
    monkeypatch.setenv("BOT_DATA_DIR", str(tmp_path))

    # Empty open_positions → loop hits the `if not market_ids: continue` branch.
    # For this branch the mock isn't called at all; we need a different
    # mechanism to trip shutdown so the test terminates.
    shutdown = asyncio.Event()
    poll_called = {"n": 0}

    async def _mock_poll(_market_ids: list[str]) -> list[_StubResolutionEvent]:
        poll_called["n"] += 1
        shutdown.set()
        return []

    monkeypatch.setattr(
        polymarket_feed_mod, "poll_market_resolutions", _mock_poll
    )

    risk = _StubRiskManager(
        equity=1000.0,
        positions={},  # empty
    )
    config = _stub_config(poll_seconds=0.01)

    # Drive the loop briefly; with empty open_positions it sleeps after
    # the early-continue. We trip shutdown manually after a brief wait.
    async def _shutdown_after_delay() -> None:
        await asyncio.sleep(0.05)
        shutdown.set()

    await asyncio.wait_for(
        asyncio.gather(
            resolution_polling_loop(risk, config, shutdown),
            _shutdown_after_delay(),
        ),
        timeout=1.0,
    )

    assert poll_called["n"] == 0  # early-continue prevented poll
    assert risk.on_settlement_calls == []
    out = tmp_path / "settlements.jsonl"
    assert (not out.exists()) or out.read_text().strip() == ""


@pytest.mark.asyncio
async def test_multiple_settlements_in_one_poll(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Multiple events in a single poll all get persisted; compute_streak
    sees the most recent (signed)."""
    monkeypatch.setenv("BOT_DATA_DIR", str(tmp_path))

    # 0xa: long YES @0.5 qty20 → resolves YES → win:  +20 - 20*0.5 = +10
    # 0xb: long NO  @0.5 qty10 → resolves YES → loss:   0 - 10*0.5 =  -5
    risk = _StubRiskManager(
        equity=1000.0,
        positions={
            "0xa_YES": _StubPosition(
                market_id="0xa", outcome="YES", quantity=20.0, avg_price=0.5
            ),
            "0xb_NO": _StubPosition(
                market_id="0xb", outcome="NO", quantity=10.0, avg_price=0.5
            ),
        },
    )
    events = [
        _StubResolutionEvent(market_id="0xa", resolved_outcome="YES"),  # win
        _StubResolutionEvent(market_id="0xb", resolved_outcome="YES"),  # loss
    ]

    await _drive_one_iteration(risk, events, monkeypatch)

    out = tmp_path / "settlements.jsonl"
    lines = [line for line in out.read_text().splitlines() if line.strip()]
    assert len(lines) == 2

    # 1 win + 1 loss → win_rate = 50.0 (0–100 scale); most recent was a loss → streak = -1
    assert compute_win_rate() == 50.0
    assert compute_streak() == -1
