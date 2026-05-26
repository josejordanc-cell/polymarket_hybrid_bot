"""
utils/risk_state_mirror.py
Profitability epic piece (2b) — RiskManager → BotState polling mirror.

Polls RiskManager every 5 seconds and atomically mirrors its public
state into BotState so ``GET /dashboard/snapshot`` returns live
``equity`` / ``open_positions`` / ``day_start_equity`` rather than the
frozen startup snapshot from ``BotState.from_risk`` (``api/state.py:217``).

Closes the LIVE-telemetry gap flagged in CHANGELOG ``[Unreleased]``
Known issues "BotState is a one-time startup snapshot" for the three
slices this loop writes. The five ``GlobalStats`` zero stubs
(``pnlMonth`` / ``pnlAllTime`` / ``winRate`` / ``totalTrades`` /
``streak`` at ``api/routes_rest.py:247-251``) remain OUT of scope —
they need a separate derived-stats pipeline.

Locked policy (2026-05-26):

- **Reads use PUBLIC ACCESSORS ONLY.** Three calls per cycle:
  ``risk.equity`` (property at ``risk_manager.py:76``),
  ``risk.open_positions`` (property at ``risk_manager.py:80`` —
  returns a defensive copy), and
  ``risk.daily_pnl_snapshot().start_equity_usd`` (method at
  ``risk_manager.py:84``). NO ``risk._state`` reads, even though
  ``BotState.from_risk`` has precedent (``api/state.py:253``) and
  reading ``_state`` would be fence-safe. Explicit policy lock per
  the (2b) design brief; reduces coupling to the RiskManager
  internal layout.
- **Writes are atomic under ``state.write_lock``.** Three writes
  per cycle: ``state.equity``, ``state.open_positions`` (atomic
  ``dict`` REPLACE — not delta-merge — so readers see a consistent
  snapshot), and ``state.day_start_equity``.
- **Poll-first ordering.** The first write happens BEFORE the
  first ``await asyncio.sleep`` so there is no 5-second stale
  window after startup. A freshly-restarted bot returns live
  values from the first snapshot request.
- **5-second interval.** Matches the snapshot read cadence
  operators see on the dashboard.

Lifecycle / cancellation pattern matches
``utils.equity_curve.equity_snapshot_loop`` exactly — same
``asyncio.CancelledError`` propagation, same defensive outer
try-except for unforeseen iteration failures, same structlog event
naming convention. Owned by ``api/server.py:run()``; cancelled in
its ``finally`` block.

Owed runtime gap: see ``.swarm-decisions.md`` ``b1-fill-runtime-update``
for the parallel (2a) deferral. (2b) has a *lighter* close criterion —
no fill is required to observe the loop running, because the loop
ticks unconditionally every 5s. Watching
``/dashboard/snapshot.equity`` and ``positions`` track ``risk.equity``
and ``risk.open_positions`` over time on the droplet IS the runtime
verification.
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from api.state import BotState

logger = structlog.get_logger(__name__)


# Default poll interval. Exposed as a module-level constant so tests
# can patch it down to drive multiple iterations quickly. Production
# code receives ``interval_sec`` as a kwarg and should not depend on
# this constant directly.
_DEFAULT_INTERVAL_SEC: int = 5


async def _mirror_once(risk: Any, state: "BotState") -> None:
    """Single mirror cycle: read RiskManager public state and write the
    three mirrored fields into BotState under ``state.write_lock``.

    Reads happen OUTSIDE the lock (RiskManager properties are
    self-contained and do not touch BotState); writes happen inside the
    lock as a single critical section so readers calling
    ``BotState.shallow_copy()`` see a consistent triple.

    Defensive: never raises. Any exception (RiskManager attribute
    error, lock contention, attribute write) is logged and swallowed
    so the parent loop keeps ticking.
    """
    try:
        # All three reads via PUBLIC accessors only. NO _state access
        # per the (2b) locked policy.
        new_equity = risk.equity
        new_open_positions = dict(risk.open_positions)
        new_day_start = risk.daily_pnl_snapshot().start_equity_usd
        async with state.write_lock:
            state.equity = new_equity
            state.open_positions = new_open_positions
            state.day_start_equity = new_day_start
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "risk_state_mirror_iteration_failed",
            error=str(exc),
        )


async def mirror_risk_to_botstate(
    risk: Any,
    state: "BotState",
    interval_sec: int = _DEFAULT_INTERVAL_SEC,
) -> None:
    """Long-lived task: poll RiskManager every ``interval_sec`` seconds
    and atomically mirror its public state into BotState.

    Lifecycle:
      - Owned by ``api/server.py:run()`` for the lifetime of the HTTP
        API server task (sibling of ``equity_snapshot_loop``).
      - Created via ``asyncio.create_task`` alongside the equity loop
        task creation.
      - Cancelled in ``run()``'s ``finally`` block; this coroutine
        propagates ``asyncio.CancelledError`` for clean shutdown.

    The poll-first write at startup is intentional and load-bearing
    for runtime smoke: it lets a freshly-restarted bot return live
    ``state.equity`` / ``state.open_positions`` /
    ``state.day_start_equity`` from ``GET /dashboard/snapshot``
    immediately, rather than waiting up to ``interval_sec`` seconds
    for the first sleep to elapse.
    """
    logger.info("risk_state_mirror_started", interval_sec=interval_sec)

    # Poll-first: write before the first sleep so there is no stale window.
    await _mirror_once(risk, state)

    while True:
        try:
            await asyncio.sleep(interval_sec)
            await _mirror_once(risk, state)
        except asyncio.CancelledError:
            logger.info("risk_state_mirror_cancelled")
            raise
        except Exception as exc:  # noqa: BLE001
            # Defensive: the loop MUST NOT die on a transient error.
            # _mirror_once already swallows its own exceptions; this
            # outer except is for unforeseen failures (e.g.
            # asyncio.sleep raising under loop-shutdown).
            logger.warning(
                "risk_state_mirror_iteration_error",
                error=str(exc),
            )
