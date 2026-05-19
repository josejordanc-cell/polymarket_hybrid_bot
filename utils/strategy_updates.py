"""
utils/strategy_updates.py
Dashboard WebSocket STRATEGY_UPDATE broadcasting infrastructure.

TODO (v0.2.9): No production call sites since revert 8bad781. Re-wire when strategy-telemetry pipeline lands per .swarm-decisions.md [B-reviewer-report 2026-05-18].

STATUS (v0.2.8 post-tag correction): NOT CURRENTLY WIRED to any emit
site. An earlier version called this from
``api/state.py:BotState.apply_strategy_patch`` (introduced d1d2276)
emitting ``StrategyConfigPatch`` fields, which violated
BOT_CONTRACT.md:609 (STRATEGY_UPDATE.updates is a partial of
StrategyCardData, not StrategyConfigPatch). That wiring was reverted
post-tag-move. See ``docs/audit/d24-emit-site-audit.md`` §6.0 (three
strategy surfaces) and §10 (correction block). The correct emit site
is the strategy-telemetry pipeline (v0.2.9): once that pipeline
produces ``StrategyCardData`` deltas, this broadcast helper is what
fires the WS message. The broadcast machinery itself is contract-
correct; only the caller is missing.

Sync entry point + auto-scheduling: ``broadcast_strategy_update`` is
SYNC so it can be called from sync state-mutation paths. It probes
for a running event loop and schedules the underlying async
``Emitter.broadcast`` via ``loop.create_task``. If no loop is running
(sync test context with no event loop), the broadcast is a silent
no-op — correct semantics for tests that aren't exercising the
broadcast path.

The module-level ``_emitter`` singleton + ``set_emitter`` / ``get_emitter``
pattern mirrors ``utils/alerts.py`` (D.2.4 step 1), ``utils/fills.py``
(D.2.4 step 2), and ``observability/metrics_exporter.py``.

Payload shape (per BOT_CONTRACT.md:413, when wired correctly):
    {"type": "STRATEGY_UPDATE",
     "payload": {"strategyId": <id>,
                 "updates": {<partial of StrategyCardData minus id>}}}
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from api.emit import Emitter

logger = structlog.get_logger(__name__)

# Module-level Emitter singleton. Set once at HTTP API server startup
# via set_emitter(). Reads via get_emitter(). When None, dashboard
# broadcast is a no-op.
_emitter: "Emitter | None" = None


def set_emitter(emitter: "Emitter | None") -> None:
    """Register (or clear) the dashboard WebSocket Emitter.

    Called from api/server.py once the Emitter is constructed. Idempotent
    — safe to call multiple times; the most recent value wins. Passing
    ``None`` clears the registration (used by tests).
    """
    global _emitter
    _emitter = emitter


def get_emitter() -> "Emitter | None":
    """Return the currently registered Emitter, or None if unset."""
    return _emitter


def broadcast_strategy_update(
    *,
    strategy_id: str,
    updates: dict[str, Any],
) -> None:
    """Broadcast a STRATEGY_UPDATE WsMessage to dashboard subscribers.

    Sync function (callable from sync state-mutation methods). If no
    Emitter is registered, no-op. If no event loop is running (sync
    test context), no-op. Otherwise schedules the underlying async
    ``Emitter.broadcast`` via ``loop.create_task`` and returns
    immediately.

    Never raises — failures are logged and swallowed. Mirrors the
    fire-and-forget semantics of ``utils/alerts.send_alert`` and
    ``utils/fills.broadcast_fill``.

    Args:
        strategy_id: The contract strategy id ("strategy1"/"strategy2"/"strategy3").
        updates: The delta dict. Per BOT_CONTRACT.md:609, allowed keys
                 are a partial of StrategyCardData: name, status,
                 pnlToday, pnlMonth, winRate, tradesToday,
                 avgHoldMinutes, lastSignal, pipelineStep, exposure,
                 sparkline. Empty {} is valid. Caller is responsible
                 for producing the delta from upstream telemetry.

    Payload shape (matches dashboard StrategyUpdateSchema and the bot's
    api.schemas.StrategyUpdate):
        {"type": "STRATEGY_UPDATE", "payload": {"strategyId": <id>,
                                                "updates": {<delta>}}}
    """
    em = get_emitter()
    if em is None:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No event loop running. Sync test context, not the HTTP server.
        # Broadcast is a no-op; that's correct semantics here.
        return
    try:
        message: dict[str, Any] = {
            "type": "STRATEGY_UPDATE",
            "payload": {
                "strategyId": strategy_id,
                "updates": dict(updates),  # defensive copy
            },
        }
        loop.create_task(em.broadcast(message))
    except Exception as exc:
        logger.warning("dashboard_strategy_update_broadcast_failed", error=str(exc))
