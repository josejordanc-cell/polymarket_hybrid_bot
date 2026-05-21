"""
utils/equity_curve.py
v0.2.9 B.7 wiring — periodic BotState.equity_history population.

Adds the snapshot path's last live carrier — until this module ran,
``equity_history`` had no production writer and the snapshot
serialized ``equity=[]`` as the final OQ-3 stub. The module exports
``equity_snapshot_loop`` — a long-lived asyncio task that wakes
hourly, checks whether today's UTC calendar day already has a point
recorded, and if not appends a fresh ``EquityPoint`` derived from
the current ``BotState.equity`` / ``day_start_equity`` values.

Locked policy (Jose 2026-05-21):

- **PATH B + date-change detection.** Background task in
  ``api/server.py``, not hooked into ``risk_manager`` (which is
  frozen per the ownership ledger; the carve-out for B.7 is not
  warranted).
- **Cadence: 1 point per UTC calendar day.** 3f locked
  ``EquityPoint.day: str`` (calendar-date string convention
  ``"YYYY-MM-DD"``), which is inherently daily-resolution. Multiple
  points per day with the same ``day`` string would render
  indistinguishably on the dashboard.
- **Loop interval: 3600 s (hourly).** Cheap on resources, robust to
  long-sleep drift across midnight UTC, and worst-case 1-hour
  staleness at the day-rollover boundary is acceptable for a
  daily-close panel.
- **Bootstrap append on loop start.** Without this, the FIRST point
  wouldn't land until UTC midnight after bot startup — making the
  runtime smoke pattern "start → wait 24h → curl" instead of
  "start → curl immediately". The bootstrap fires
  ``_maybe_append_today`` before the first ``asyncio.sleep`` so the
  snapshot reflects today's equity from boot+1s.
- **No WS broadcast.** Mirrors the B.8 logs decision: Zod
  ``WsMessageSchema`` has no ``EQUITY`` variant; the snapshot is
  the sole transport.

The append is bounded by ``BotState.equity_history``'s
``deque(maxlen=200)`` (api/state.py:149) — 200 days of daily-close
history. Older points naturally drop off the left edge.

PnL definition: ``state.equity - state.day_start_equity`` — matches
``globalStats.pnlToday`` semantics. ``day_start_equity`` is
maintained by ``risk_manager._maybe_reset_day`` (read-only access
here; no risk_manager unfreeze needed).
"""
from __future__ import annotations

import asyncio
from itertools import groupby
from typing import TYPE_CHECKING, List, Sequence

import structlog

from api.schemas import EquityPoint, MonthlyPnl
from utils.types import utc_now

if TYPE_CHECKING:
    from api.state import BotState

logger = structlog.get_logger(__name__)


# Default loop interval. Exposed as a module-level constant so tests
# can patch it down to drive multiple iterations quickly. Production
# code receives ``interval_sec`` as a kwarg and should not depend on
# this constant directly.
_DEFAULT_INTERVAL_SEC: int = 3600


async def _maybe_append_today(state: "BotState") -> None:
    """Append today's EquityPoint to ``state.equity_history`` IF the
    current UTC calendar day isn't already represented.

    The compare-and-append happens under ``state.write_lock`` so a
    snapshot read can't observe a half-mutated deque (matters less
    here than for ``recent_*`` but the lock is cheap and the
    consistency guarantee is the same).

    Idempotent within a UTC day — calling repeatedly across the same
    day is a no-op. Date rollover (UTC midnight) is detected by
    string comparison; no clock-aware scheduling needed.

    Defensive: never raises. Any exception (Pydantic validation,
    lock contention, deque write) is logged and swallowed so the
    parent loop keeps ticking.
    """
    try:
        today_iso = utc_now().date().isoformat()
        async with state.write_lock:
            last_day = (
                state.equity_history[-1].day
                if state.equity_history
                else None
            )
            if last_day == today_iso:
                # Already recorded today; no-op.
                return
            point = EquityPoint(
                day=today_iso,
                equity=state.equity,
                pnl=state.equity - state.day_start_equity,
            )
            state.equity_history.append(point)
        logger.info(
            "equity_curve_point_appended",
            day=today_iso,
            equity=point.equity,
            pnl=point.pnl,
            curve_len=len(state.equity_history),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "equity_curve_append_failed",
            error=str(exc),
        )


async def equity_snapshot_loop(
    state: "BotState",
    interval_sec: int = _DEFAULT_INTERVAL_SEC,
) -> None:
    """Long-lived task: bootstrap-append today's point at startup,
    then poll hourly for UTC-day rollovers.

    Lifecycle:
      - Owned by ``api/server.py:run()`` for the lifetime of the
        HTTP API server task.
      - Created via ``asyncio.create_task`` after the
        ``set_state(state)`` wiring (so ``get_state()`` would return
        the same state object — though this loop reads its ``state``
        argument directly and doesn't use the singleton).
      - Cancelled in ``run()``'s ``finally`` block; this coroutine
        propagates ``asyncio.CancelledError`` for clean shutdown.

    The bootstrap append is intentional and load-bearing for runtime
    smoke: it lets a freshly-restarted bot return a non-empty
    ``equity_history`` from ``GET /dashboard/snapshot`` immediately,
    rather than waiting until the next UTC midnight. Without the
    bootstrap, the first ``_maybe_append_today`` call wouldn't fire
    until ``await asyncio.sleep(interval_sec)`` returned (worst case
    1 hour for the hourly default cadence).
    """
    logger.info("equity_snapshot_loop_started", interval_sec=interval_sec)

    # Bootstrap: ensure today's point exists immediately on loop start.
    await _maybe_append_today(state)

    while True:
        try:
            await asyncio.sleep(interval_sec)
            await _maybe_append_today(state)
        except asyncio.CancelledError:
            logger.info("equity_snapshot_loop_cancelled")
            raise
        except Exception as exc:  # noqa: BLE001
            # Defensive: the loop MUST NOT die on a transient error.
            # _maybe_append_today already swallows its own exceptions;
            # this outer except is for unforeseen failures (e.g.
            # asyncio.sleep raising under loop-shutdown).
            logger.warning(
                "equity_snapshot_loop_iteration_error",
                error=str(exc),
            )


# ---------------------------------------------------------------------------
# Monthly-PnL aggregation (v0.2.9 v029-monthly-pnl-wiring)
# ---------------------------------------------------------------------------


def compute_monthly_pnl(
    equity_history: Sequence[EquityPoint],
) -> List[MonthlyPnl]:
    """Aggregate ``equity_history`` into per-month PnL rows.

    Groups EquityPoint instances by their ``day`` field's month prefix
    (``"YYYY-MM"`` from the ``"YYYY-MM-DD"`` calendar-date convention).
    For each month with **≥2 points** (lock 6), emits a ``MonthlyPnl``
    with PnL computed as ``last_equity_in_month - first_equity_in_month``
    (lock 5). Months with fewer than 2 points are silently dropped
    (insufficient data — a single point gives an uninformative
    zero-PnL row). Output is sorted ascending by month string
    (lock 3) — matches ``equity_history`` natural iteration order.

    Pure function; no I/O; no logging. Defensive on empty input
    (returns ``[]``). Computation cost: O(n) over the deque,
    negligible at n ≤ 200. Mirrors B.1 / B.7 helper-in-utils pattern.

    Coordinator-locked decisions (v029-monthly-pnl-wiring):
      1. ``trades`` field removed from ``MonthlyPnl`` (forward-only
         schema reconcile against Zod canonical). Helper signature
         doesn't include it.
      2. month format: ``"YYYY-MM"``.
      3. sort order: ascending.
      4. helper home: this module (``utils/equity_curve.py``).
      5. PnL semantic: ``last_equity_in_month - first_equity_in_month``.
      6. minimum samples per month: ≥2 (dropped otherwise).
      7. history depth: no explicit cap — whatever ``equity_history``
         naturally yields (~6.5 months at deque ``maxlen=200``).
    """
    if not equity_history:
        return []

    # Group by month-prefix of each EquityPoint.day ("YYYY-MM-DD" → "YYYY-MM").
    # equity_history is already in chronological order (deque insertion order
    # from equity_snapshot_loop), so groupby groups contiguous same-month runs.
    rows: List[MonthlyPnl] = []
    for month, group in groupby(equity_history, key=lambda pt: pt.day[:7]):
        points = list(group)
        if len(points) < 2:
            # Lock 6: <2 points in this month → drop (uninformative).
            continue
        # Lock 5: last - first equity within the month.
        rows.append(
            MonthlyPnl(
                month=month,
                pnl=points[-1].equity - points[0].equity,
            )
        )
    # Lock 3: ascending sort. equity_history's natural order is already
    # chronological, but a defensive sort here makes the helper robust
    # against any future caller that passes an unsorted sequence.
    rows.sort(key=lambda r: r.month)
    return rows
