"""
utils/fills.py
Dashboard WebSocket FILL broadcasting (D.2.4 step 2).

When the HTTP API server is running, ``set_emitter`` is called from
``api/server.py`` to register the Emitter. Call sites in the strategy
loop (``main.py:_exec_and_record``) invoke ``broadcast_fill`` after a
fill is reconciled. If no Emitter has been registered (e.g. unit tests
not exercising broadcast), the broadcast step silently no-ops.

The module-level ``_emitter`` singleton + ``set_emitter`` / ``get_emitter``
pattern mirrors ``utils/alerts.py`` (D.2.4 step 1) and
``observability/metrics_exporter.py``. See
docs/audit/d24-emit-site-audit.md §10 for context.

Payload shape matches dashboard ``FillSchema`` in
``components/dashboard/schemas.ts``. See d24-emit-site-audit.md §10
(FILL drift discovery) for the full field-by-field reconciliation.
"""
from __future__ import annotations

import asyncio
import time as _time_mod
import uuid
from typing import TYPE_CHECKING, Any, Literal, Optional

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


async def broadcast_fill(
    *,
    market_id: str,
    action: Literal["UP", "DOWN"],
    price: float,
    size: float,
    strategy: str,
    pnl: Optional[float] = None,
) -> None:
    """Broadcast a FILL WsMessage to dashboard subscribers if an Emitter
    is registered. Payload shape matches the dashboard's ``FillSchema``
    per docs/audit/d24-emit-site-audit.md §10.

    Never raises — failures are logged and swallowed. Mirrors the
    fire-and-forget semantics of ``utils/alerts.send_alert``.

    Field mapping:
      - ``id``        : minted as ``uuid4().hex`` (dashboard uses opaque IDs)
      - ``time``      : current wall-clock as ``HH:MM:SS`` (dashboard format
                        per ``components/dashboard/mock-data.ts:178``)
      - ``symbol``    : ``market_id[:12]`` — display-only mapping, not a
                        semantic claim that Polymarket market IDs are
                        trading-symbol pairs. Future work may map to a
                        real display name once Polymarket market metadata
                        is plumbed through.
      - ``action``    : UP/DOWN (matches dashboard enum)
      - ``price``     : fill price
      - ``size``      : filled size (dashboard field is ``size``; bot's
                        internal name was previously ``quantity``)
      - ``pnl``       : Optional/nullable — at fill time the bot does NOT
                        know realized pnl (computed downstream in equity
                        reconciliation). Emitted as ``None`` here. See
                        d24-emit-site-audit.md §10 for the Level-1
                        decision rationale.
      - ``status``    : hardcoded ``"filled"`` at this emit site by
                        construction (guard at main.py:516 only emits
                        when ``result.accepted and result.filled_size > 0``).
      - ``strategy``  : strategy id string
    """
    em = get_emitter()
    if em is None:
        return
    try:
        payload: dict[str, Any] = {
            "id": uuid.uuid4().hex,
            "time": _time_mod.strftime("%H:%M:%S", _time_mod.localtime()),
            "symbol": market_id[:12],
            "action": action,
            "price": float(price),
            "size": float(size),
            "pnl": float(pnl) if pnl is not None else None,
            "status": "filled",
            "strategy": str(strategy),
        }
        await em.broadcast({"type": "FILL", "payload": payload})
    except Exception as exc:
        logger.warning("dashboard_fill_broadcast_failed", error=str(exc))
