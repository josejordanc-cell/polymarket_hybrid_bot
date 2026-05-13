"""
api/server.py — Entry-point coroutine for the bot HTTP/WebSocket module.

Invoked from main.py:run_bot() via asyncio.create_task(api_run(...)) when
config.http_api.enabled is True. Runs an aiohttp Application that serves
the three REST routes registered by api.routes_rest.setup_routes plus
the GET /ws WebSocket route registered by api.routes_ws.ws_handler.

The Emitter is owned by ``run()`` and lives for the lifetime of the
server task. It is attached to the Application via the typed
``EMITTER_KEY`` declared in ``api/routes_ws`` so the handler can read
it on every connection. ``CONFIG_KEY`` carries the BotConfig that
``verify_ws_ticket`` needs to read the HS256 shared secret.

D.2.3 sets up the broadcaster infrastructure; D.2.4 will wire
``emitter.broadcast(...)`` into the strategy loop's fill / alert /
strategy-update paths.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog
from aiohttp import web

from api.emit import Emitter
from api.routes_rest import setup_routes
from api.routes_ws import CONFIG_KEY, EMITTER_KEY, ws_handler
from api.state import BotState

logger = structlog.get_logger(__name__)


# Polling interval for the shutdown-event check. aiohttp's AppRunner does
# not expose a native event-driven shutdown hook at this layer, so we poll.
# 0.5s is low overhead and gives sub-second shutdown latency in practice.
_SHUTDOWN_POLL_INTERVAL_S: float = 0.5


async def run(
    state: BotState,
    config: Any,
    shutdown: asyncio.Event,
) -> None:
    """Start the aiohttp HTTP server and run until ``shutdown`` is set.

    Behavior:
      - Builds an ``aiohttp.web.Application``, delegates route registration
        to ``api.routes_rest.setup_routes(app, state, config)``.
      - Reads ``config.api.bind_host`` and ``config.api.port`` via
        ``getattr`` with safe defaults (``"0.0.0.0"`` / ``8080``). Step 6
        wires the real nested ``HttpApiConfig`` model into BotConfig; until
        then the defaults let this module be imported and exercised stand-
        alone.
      - Starts an ``AppRunner`` + ``TCPSite`` and polls ``shutdown`` at
        ``_SHUTDOWN_POLL_INTERVAL_S`` intervals.
      - Logs ``api_server_started`` / ``api_server_stopped`` info events
        and ``api_server_fatal_error`` critical-with-exc_info on unexpected
        failure (D.2.1 §D-3 crash-behavior paragraph). Re-raises so the
        parent task gather sees the failure.
      - Cleans up the runner unconditionally in ``finally``.

    The signature is stable across D.2.2 / D.2.3 / D.2.4 — D.2.3 will add a
    second `setup_routes` call for WS routes, without changing this entry.
    """
    app = web.Application()
    setup_routes(app, state, config)

    # Emitter + WS route are owned by run() for the lifetime of the
    # server task. The emitter is attached to the Application via
    # typed AppKeys so api.routes_ws.ws_handler can read it on each
    # upgrade. D.2.4 will plumb a reference into the strategy loop
    # (likely through BotState or a top-level wiring) to make
    # broadcast() calls from the fill / alert / strategy-update paths.
    emitter = Emitter()
    app[EMITTER_KEY] = emitter
    app[CONFIG_KEY] = config
    app.router.add_get("/ws", ws_handler)

    api_cfg = getattr(config, "api", None)
    port: int = getattr(api_cfg, "port", 8080)
    bind_host: str = getattr(api_cfg, "bind_host", "0.0.0.0")

    runner = web.AppRunner(app)
    try:
        await runner.setup()
        site = web.TCPSite(runner, bind_host, port)
        await site.start()
        logger.info("api_server_started", host=bind_host, port=port)

        while not shutdown.is_set():
            await asyncio.sleep(_SHUTDOWN_POLL_INTERVAL_S)

    except Exception:
        logger.critical("api_server_fatal_error", exc_info=True)
        raise
    finally:
        await runner.cleanup()
        logger.info("api_server_stopped")
