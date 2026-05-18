"""
utils/strategy_updates.py
Dashboard WebSocket STRATEGY_UPDATE broadcasting (D.2.4 step 3).

When the HTTP API server is running, ``set_emitter`` is called from
``api/server.py`` to register the Emitter. The single call site is
``api/state.py:BotState.apply_strategy_patch`` (audit §5.3
recommendation), which invokes ``broadcast_strategy_update`` after
applying a ``StrategyConfigPatch``. If no Emitter has been registered
(e.g. unit tests not exercising broadcast), the broadcast step
silently no-ops.

Sync entry point + auto-scheduling: unlike utils/fills.broadcast_fill
(async, called from the async strategy loop), this module's
``broadcast_strategy_update`` is SYNC because ``apply_strategy_patch``
is sync and called from both async HTTP route handlers and sync test
setup paths. Sync callers can't ``await`` the underlying async
``Emitter.broadcast``, so we schedule it via the currently-running
event loop's ``create_task``. If no loop is running (i.e. called from
a sync test outside of any event loop), the broadcast is a no-op —
that's the correct semantics for a test that isn't exercising the
broadcast path.

The module-level ``_emitter`` singleton + ``set_emitter`` / ``get_emitter``
pattern mirrors ``utils/alerts.py`` (D.2.4 step 1), ``utils/fills.py``
(D.2.4 step 2), and ``observability/metrics_exporter.py``. See
docs/audit/d24-emit-site-audit.md §10 for context.

Payload shape: ``{type: "STRATEGY_UPDATE", payload: {strategyId, updates}}``
where ``updates`` contains only the StrategyConfigPatch fields that were
non-None (the per-strategy delta semantic the dashboard expects). The
bot's StrategyUpdateFields is intentionally narrower than the dashboard's
permissive StrategyCardUpdatesSchema — see d24-emit-site-audit.md §10
(STRATEGY_UPDATE drift, bot-narrower paragraph) for rationale.
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
        updates: The delta dict. Should contain ONLY non-None fields the
                 patch actually changed. Caller is responsible for
                 filtering: typically ``patch.model_dump(exclude_none=True,
                 exclude={"id"})`` from inside ``apply_strategy_patch``.

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
