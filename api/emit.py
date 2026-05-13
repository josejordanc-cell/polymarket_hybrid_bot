"""
api/emit.py — WebSocket broadcaster for the bot HTTP/WS module.

Implements D.2.3 Step 1 per docs/architecture/D.2.1-http-module.md §D-5
(WebSocket emission ordering + back-pressure) and SESSION-BRIEF D.2.3.

The Emitter owns a registry of connected WebSocket clients keyed by an
opaque connection_id. Each client has a bounded ``asyncio.Queue``. The
strategy loop (D.2.4) and unit tests call ``broadcast()``; the Emitter
fans the message out to every connected client via ``put_nowait``.

Back-pressure policy (D.2.3 simplification of D.2.1 §D-5):
  - Per-client queue of ``max_queue_per_client`` (default 256) messages.
  - On ``asyncio.QueueFull`` during ``broadcast()``, increment
    ``_consecutive_full[connection_id]``.
  - On a successful ``put_nowait``, reset
    ``_consecutive_full[connection_id]`` to 0.
  - After ``_BACKPRESSURE_STRIKES`` (3) consecutive full events for a
    single client, schedule that client's ``ws.close()`` +
    ``unregister()`` in the background. Other clients are unaffected.

The Emitter does NOT mutate ``BotState``. It is owned by
``api/server.py`` and attached to ``app["emitter"]`` for the lifetime
of the API task. D.2.4 will wire ``broadcast()`` into the strategy
loop's fill / alert / strategy-update paths; D.2.3 only sets up the
infrastructure and exercises it from tests.
"""
from __future__ import annotations

import asyncio
from typing import Any

import structlog
from aiohttp import web

logger = structlog.get_logger(__name__)


# Per-spec disconnect threshold. Three consecutive QueueFull events on
# the same client trigger a backpressure-driven disconnect.
_BACKPRESSURE_STRIKES: int = 3

# Default per-client queue depth. Brief D.2.3 Step 1 specifies 256;
# D.2.1 §D-5 discusses a 1000-deep variant — the smaller value here is
# the canonical D.2.3 default and is overridable via the constructor.
_DEFAULT_MAX_QUEUE: int = 256


class Emitter:
    """Owns connected WS clients and broadcasts JSON messages to them.

    Instances are created once per running API server task and attached
    to the aiohttp Application via ``app["emitter"]``. The Emitter does
    not perform any network I/O on its own — it appends messages to
    per-client queues. A companion ``stream_to_client()`` coroutine,
    run as a task per connection by ``routes_ws.ws_handler``, drains
    the queue and writes to the WebSocket.

    All methods are async-safe under the single-threaded asyncio event
    loop. A coarse ``asyncio.Lock`` guards registry mutations so a
    concurrent broadcast cannot iterate the dict mid-(un)register.
    """

    def __init__(self, max_queue_per_client: int = _DEFAULT_MAX_QUEUE) -> None:
        self._max_queue_per_client: int = max_queue_per_client
        self._clients: dict[
            int, tuple[web.WebSocketResponse, asyncio.Queue]
        ] = {}
        self._consecutive_full: dict[int, int] = {}
        self._next_connection_id: int = 1
        self._lock: asyncio.Lock = asyncio.Lock()

    @property
    def client_count(self) -> int:
        """Number of currently-registered WS clients."""
        return len(self._clients)

    async def register(self, ws: web.WebSocketResponse) -> int:
        """Register *ws* and return a fresh opaque ``connection_id``."""
        async with self._lock:
            connection_id = self._next_connection_id
            self._next_connection_id += 1
            queue: asyncio.Queue = asyncio.Queue(
                maxsize=self._max_queue_per_client
            )
            self._clients[connection_id] = (ws, queue)
            self._consecutive_full[connection_id] = 0
        logger.info(
            "ws_client_registered",
            connection_id=connection_id,
            total=self.client_count,
        )
        return connection_id

    async def unregister(self, connection_id: int) -> None:
        """Remove *connection_id* from the registry. Idempotent."""
        async with self._lock:
            entry = self._clients.pop(connection_id, None)
            self._consecutive_full.pop(connection_id, None)
        if entry is not None:
            logger.info(
                "ws_client_unregistered",
                connection_id=connection_id,
                total=self.client_count,
            )

    async def broadcast(self, message: dict[str, Any]) -> None:
        """Fan-out *message* to every registered client.

        For each client, attempt ``queue.put_nowait(message)``. On
        ``asyncio.QueueFull`` the per-client strike counter is
        incremented; after ``_BACKPRESSURE_STRIKES`` consecutive full
        events that client is scheduled for background disconnect.
        Other clients are unaffected.

        ``message`` must be JSON-serialisable — the per-client streamer
        passes it to ``ws.send_json``.
        """
        async with self._lock:
            snapshot = list(self._clients.items())

        to_disconnect: list[int] = []
        for connection_id, (_ws, queue) in snapshot:
            try:
                queue.put_nowait(message)
                self._consecutive_full[connection_id] = 0
            except asyncio.QueueFull:
                strikes = self._consecutive_full.get(connection_id, 0) + 1
                self._consecutive_full[connection_id] = strikes
                logger.warning(
                    "ws_queue_full",
                    connection_id=connection_id,
                    strikes=strikes,
                )
                if strikes >= _BACKPRESSURE_STRIKES:
                    to_disconnect.append(connection_id)

        for connection_id in to_disconnect:
            logger.warning(
                "ws_client_backpressure_disconnect",
                connection_id=connection_id,
                strikes=_BACKPRESSURE_STRIKES,
            )
            asyncio.create_task(
                self._disconnect_for_backpressure(connection_id),
                name=f"ws_backpressure_disconnect_{connection_id}",
            )

    async def _disconnect_for_backpressure(self, connection_id: int) -> None:
        """Close a slow client's WebSocket and unregister it."""
        entry = self._clients.get(connection_id)
        if entry is None:
            return
        ws, _queue = entry
        try:
            await ws.close(code=1011, message=b"backpressure")
        except Exception:  # noqa: BLE001
            logger.debug(
                "ws_backpressure_close_error",
                connection_id=connection_id,
                exc_info=True,
            )
        await self.unregister(connection_id)

    async def stream_to_client(
        self,
        connection_id: int,
        ws: web.WebSocketResponse,
    ) -> None:
        """Drain *connection_id*'s outbound queue, writing JSON to *ws*.

        Run as a per-connection ``asyncio.Task`` by
        ``routes_ws.ws_handler``. Exits cleanly on cancellation, on a
        WS that has already closed, or on a transport-level send
        failure. Does NOT unregister the client — the WS handler owns
        the lifecycle and calls ``emitter.unregister()`` in its own
        ``finally`` block.
        """
        entry = self._clients.get(connection_id)
        if entry is None:
            return
        _ws, queue = entry
        try:
            while True:
                msg = await queue.get()
                if ws.closed:
                    return
                await ws.send_json(msg)
        except asyncio.CancelledError:
            raise
        except ConnectionResetError:
            logger.info(
                "ws_send_connection_reset",
                connection_id=connection_id,
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "ws_send_error",
                connection_id=connection_id,
                exc_info=True,
            )
