"""
utils/alerts.py
Lightweight Telegram and Discord webhook alerting.

Configure via .env:
    BOT_TELEGRAM_TOKEN=<bot_token>
    BOT_TELEGRAM_CHAT_ID=<chat_id>
    BOT_DISCORD_WEBHOOK_URL=<webhook_url>

Both are optional — alerts silently no-op if not configured.
"""
from __future__ import annotations

import os
from typing import Literal

import structlog

logger = structlog.get_logger(__name__)

AlertLevel = Literal["info", "warning", "critical"]

_LEVEL_EMOJI = {
    "info": "ℹ️",
    "warning": "⚠️",
    "critical": "🚨",
}


async def send_alert(
    message: str,
    level: AlertLevel = "info",
    source: str = "polymarket-bot",
) -> None:
    """
    Fire-and-forget alert to Telegram and/or Discord.
    Never raises — failures are logged and swallowed.
    """
    emoji = _LEVEL_EMOJI.get(level, "ℹ️")
    full_msg = f"{emoji} [{source}] {message}"

    import asyncio
    await asyncio.gather(
        _send_telegram(full_msg),
        _send_discord(full_msg),
        return_exceptions=True,
    )


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
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
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

