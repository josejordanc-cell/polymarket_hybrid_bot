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
    """Broadcast an ALERT WsMessage to dashboard subscribers if an Emitter
    is registered. Payload shape matches the dashboard's
    ``AlertItemSchema`` per docs/audit/d24-emit-site-audit.md §10."""
    em = get_emitter()
    if em is None:
        return
    try:
        payload = {
            "id": uuid.uuid4().hex,
            "time": _time_mod.strftime("%H:%M:%S", _time_mod.localtime()),
            "severity": level,
            "source": source,
            "message": message,
        }
        await em.broadcast({"type": "ALERT", "payload": payload})
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
