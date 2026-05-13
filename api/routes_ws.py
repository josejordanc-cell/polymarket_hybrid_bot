"""
api/routes_ws.py — WebSocket route handler for the bot HTTP module.

Implements D.2.3 Step 2 per docs/architecture/D.2.1-http-module.md §D-2
(file layout) and §D-3a (auth model). Exposes a single coroutine,
``ws_handler``, registered at ``GET /ws`` by ``api/server.py`` in Step 4.

Auth (D.2.1 §D-3a):
  - The client presents an HS256 JWT ticket as the ``?ticket=<jwt>``
    query parameter.
  - ``api.auth.verify_ws_ticket`` enforces signature, algorithm
    allowlist, ``exp``, ``iat`` presence, and single-use ``sub``
    semantics. Replayed tickets fail.
  - On any auth failure (missing ticket, bad signature, expired,
    consumed nonce, unknown algorithm), the upgrade still completes so
    the client observes a WebSocket close frame with code ``4001``
    (``auth_required``) per ``BOT_CONTRACT.md`` §3.2, then the
    response returns immediately without registering with the emitter.

Lifecycle on successful auth:
  1. Register with ``request.app["emitter"]`` to obtain a
     ``connection_id`` and a per-client outbound queue.
  2. Spawn an ``asyncio.Task`` that drains the queue to the WS via
     ``Emitter.stream_to_client``. The streamer is the only writer to
     ``ws.send_json``.
  3. Run an inbound loop. Client→server messages are not part of the
     D.2.3 protocol — they are logged at debug and dropped. Future
     work (PING/PONG, subscription filters, ack frames) lands here.
  4. On disconnect (clean close, error, cancellation), cancel the
     streamer task, await its exit, then unregister the connection.
     The handler's ``finally`` block owns the unregister lifecycle —
     ``Emitter.stream_to_client`` does NOT unregister itself.

Dependencies are read from ``request.app`` via typed aiohttp ``AppKey``s:
    request.app[EMITTER_KEY] -> api.emit.Emitter
    request.app[CONFIG_KEY]  -> BotConfig (passed as Any to verify_ws_ticket)
Both keys are populated by ``api/server.py:run()``.
"""
from __future__ import annotations

import asyncio

import structlog
from aiohttp import web

from api.auth import verify_ws_ticket
from api.emit import Emitter

logger = structlog.get_logger(__name__)


# Typed application keys (aiohttp ≥3.9). Defined here — alongside the
# handler that consumes them — so api/server.py imports them rather
# than declaring its own loose string keys. Using AppKey silences the
# NotAppKeyWarning emitted by aiohttp when string keys are used.
EMITTER_KEY: web.AppKey[Emitter] = web.AppKey("emitter", Emitter)
CONFIG_KEY: web.AppKey = web.AppKey("config")


# Per BOT_CONTRACT.md §3.2: the 4001 close code is reserved for
# "auth_required" — applied uniformly for missing-ticket and
# invalid-ticket cases so the failure mode is indistinguishable
# (don't leak whether the ticket was absent vs malformed).
_WS_CLOSE_AUTH_REQUIRED: int = 4001
_WS_CLOSE_AUTH_MSG: bytes = b"auth_required"


async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    """Accept a WebSocket upgrade at ``GET /ws`` and stream broadcasts."""
    emitter: Emitter = request.app[EMITTER_KEY]
    config = request.app[CONFIG_KEY]

    ws = web.WebSocketResponse()
    await ws.prepare(request)

    ticket = request.query.get("ticket")
    if not ticket:
        logger.info("ws_auth_missing_ticket", remote=request.remote)
        await ws.close(
            code=_WS_CLOSE_AUTH_REQUIRED, message=_WS_CLOSE_AUTH_MSG
        )
        return ws

    if not verify_ws_ticket(ticket, config):
        logger.info("ws_auth_invalid_ticket", remote=request.remote)
        await ws.close(
            code=_WS_CLOSE_AUTH_REQUIRED, message=_WS_CLOSE_AUTH_MSG
        )
        return ws

    connection_id = await emitter.register(ws)
    stream_task: asyncio.Task = asyncio.create_task(
        emitter.stream_to_client(connection_id, ws),
        name=f"ws_stream_{connection_id}",
    )

    try:
        async for msg in ws:
            logger.debug(
                "ws_inbound_dropped",
                connection_id=connection_id,
                msg_type=getattr(msg.type, "name", str(msg.type)),
            )
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        logger.warning(
            "ws_inbound_loop_error",
            connection_id=connection_id,
            exc_info=True,
        )
    finally:
        stream_task.cancel()
        try:
            await stream_task
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            logger.debug(
                "ws_stream_task_exit_error",
                connection_id=connection_id,
                exc_info=True,
            )
        await emitter.unregister(connection_id)

    return ws
