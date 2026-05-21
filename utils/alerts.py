"""
utils/alerts.py
Lightweight Telegram and Discord webhook alerting, plus dashboard
WebSocket ALERT broadcasting (D.2.4 step 1).

Configure via .env:
    BOT_TELEGRAM_TOKEN=<bot_token>
    BOT_TELEGRAM_CHAT_ID=<chat_id>
    BOT_DISCORD_WEBHOOK_URL=<webhook_url>

Telegram and Discord are optional — alerts silently no-op if not
configured. Dashboard broadcast is also optional: if no Emitter has
been registered via ``set_emitter`` (e.g. when the HTTP API server is
not running, or in unit tests that do not need broadcast), the
broadcast step silently no-ops.

The module-level ``_emitter`` singleton + ``set_emitter`` /
``get_emitter`` pattern mirrors ``observability/metrics_exporter.py``;
see docs/audit/d24-emit-site-audit.md §10 for context.
"""
from __future__ import annotations

import os
import time as _time_mod
import uuid
from typing import TYPE_CHECKING, Literal

import structlog

from api.schemas import AlertItem
from api.state import get_state

if TYPE_CHECKING:
    from api.emit import Emitter

logger = structlog.get_logger(__name__)

AlertLevel = Literal["info", "warning", "critical"]

_LEVEL_EMOJI = {
    "info": "ℹ️",
    "warning": "⚠️",
    "critical": "🚨",
}

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


async def send_alert(
    message: str,
    level: AlertLevel = "info",
    source: str = "polymarket-bot",
) -> None:
    """
    Fire-and-forget alert to Telegram, Discord, and the dashboard
    WebSocket (if an Emitter is registered).
    Never raises — failures are logged and swallowed.
    """
    emoji = _LEVEL_EMOJI.get(level, "ℹ️")
    full_msg = f"{emoji} [{source}] {message}"

    import asyncio
    await asyncio.gather(
        _send_telegram(full_msg),
        _send_discord(full_msg),
        _broadcast_dashboard(message, level, source),
        return_exceptions=True,
    )


async def _broadcast_dashboard(
    message: str,
    level: AlertLevel,
    source: str,
) -> None:
    """Broadcast an ALERT WsMessage to dashboard subscribers AND append
    to ``BotState.recent_alerts`` so the snapshot history reflects the
    alert. Payload shape matches the dashboard's ``AlertItemSchema`` per
    docs/audit/d24-emit-site-audit.md §10 and v0.2.9 sub-phase 3f
    (AlertItem 5-field canonical).

    v0.2.9 B.9 wiring: state-side append runs BEFORE the WS broadcast so
    a reconnecting client doing ``GET /dashboard/snapshot`` sees the
    alert in ``snap.recent_alerts`` even if the broadcast hasn't reached
    the WS subscriber yet. Both the state-write and the broadcast
    silently no-op when their respective singleton is unregistered
    (headless / unit-test environments). The state-append happens under
    ``state.write_lock`` for atomicity with concurrent snapshot reads.

    Both write paths are wrapped in a single try/except to preserve the
    fire-and-forget guarantee of ``send_alert``: an exception in either
    the state append or the WS broadcast is logged and swallowed, never
    raised to the caller.
    """
    em = get_emitter()
    state = get_state()
    if em is None and state is None:
        # Nothing to do — both consumers absent.
        return
    try:
        # Build the AlertItem Pydantic once; it's the single source of
        # truth for both the state-side append and the wire dict
        # (broadcasted via .model_dump). Pydantic validation guards
        # against any future drift from the contract.
        alert_item = AlertItem(
            id=uuid.uuid4().hex,
            time=_time_mod.strftime("%H:%M:%S", _time_mod.localtime()),
            severity=level,
            source=source,
            message=message,
        )

        # State-write FIRST: snapshot history must reflect the alert
        # before the broadcast races a reconnect.
        if state is not None:
            async with state.write_lock:
                state.recent_alerts.append(alert_item)

        if em is not None:
            await em.broadcast(
                {"type": "ALERT", "payload": alert_item.model_dump(mode="json")}
            )
    except Exception as exc:
        logger.warning("dashboard_alert_broadcast_failed", error=str(exc))


async def _send_telegram(text: str) -> None:
    token = os.environ.get("BOT_TELEGRAM_TOKEN", "")
    chat_id = os.environ.get("BOT_TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return
    try:
        import aiohttp
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        async with aiohttp.ClientSession() as session:
            await session.post(
                url,
                json={"chat_id": chat_id, "text": text},
                timeout=aiohttp.ClientTimeout(total=5),
            )
    except Exception as exc:
        logger.warning("telegram_alert_failed", error=str(exc))


async def _send_discord(text: str) -> None:
    webhook = os.environ.get("BOT_DISCORD_WEBHOOK_URL", "")
    if not webhook:
        return
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            await session.post(
                webhook,
                json={"content": text},
                timeout=aiohttp.ClientTimeout(total=5),
            )
    except Exception as exc:
        logger.warning("discord_alert_failed", error=str(exc))
