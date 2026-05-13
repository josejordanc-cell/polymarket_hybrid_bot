"""tests/api/test_emit.py — Unit tests for api/emit.py Emitter.

Covers:
    register / unregister cycles                                — 3 tests
    broadcast fan-out to N=0, N=1, N=3                          — 3 tests
    backpressure: strike-counter, 3-strike disconnect, reset    — 3 tests
    other clients unaffected when a peer is dropped             — 1 test
Total: 10 tests.

The WebSocketResponse is mocked with AsyncMock — these are unit tests
that exercise the Emitter in isolation; the handler-level integration
path lives in tests/api/test_routes_ws.py.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from api.emit import _BACKPRESSURE_STRIKES, Emitter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_ws() -> AsyncMock:
    """Build an AsyncMock that mimics aiohttp.web.WebSocketResponse.

    ``closed`` is a plain bool, not an awaitable; ``send_json`` and
    ``close`` are awaitable coroutines via AsyncMock's default.
    """
    ws = AsyncMock()
    ws.closed = False
    return ws


async def _wait_for(predicate, *, timeout: float = 1.0, interval: float = 0.01):
    """Poll *predicate* until it returns True or *timeout* elapses."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return False


# ---------------------------------------------------------------------------
# TestRegisterUnregister
# ---------------------------------------------------------------------------


class TestRegisterUnregister:
    async def test_register_returns_increasing_ids(self) -> None:
        emitter = Emitter()
        id1 = await emitter.register(_make_mock_ws())
        id2 = await emitter.register(_make_mock_ws())
        id3 = await emitter.register(_make_mock_ws())
        assert id1 < id2 < id3
        assert emitter.client_count == 3

    async def test_unregister_removes_client(self) -> None:
        emitter = Emitter()
        cid = await emitter.register(_make_mock_ws())
        assert emitter.client_count == 1
        await emitter.unregister(cid)
        assert emitter.client_count == 0

    async def test_unregister_unknown_id_is_idempotent(self) -> None:
        emitter = Emitter()
        await emitter.unregister(9999)
        await emitter.unregister(9999)
        assert emitter.client_count == 0


# ---------------------------------------------------------------------------
# TestBroadcastFanOut
# ---------------------------------------------------------------------------


class TestBroadcastFanOut:
    async def test_broadcast_to_zero_clients(self) -> None:
        emitter = Emitter()
        await emitter.broadcast({"type": "TEST"})
        assert emitter.client_count == 0

    async def test_broadcast_to_one_client(self) -> None:
        emitter = Emitter()
        cid = await emitter.register(_make_mock_ws())
        msg = {"type": "TEST", "n": 1}
        await emitter.broadcast(msg)
        _, queue = emitter._clients[cid]
        received = await asyncio.wait_for(queue.get(), timeout=1.0)
        assert received == msg

    async def test_broadcast_to_three_clients(self) -> None:
        emitter = Emitter()
        cids = [await emitter.register(_make_mock_ws()) for _ in range(3)]
        msg = {"type": "TEST", "n": 2}
        await emitter.broadcast(msg)
        for cid in cids:
            _, queue = emitter._clients[cid]
            received = await asyncio.wait_for(queue.get(), timeout=1.0)
            assert received == msg


# ---------------------------------------------------------------------------
# TestBackpressure
# ---------------------------------------------------------------------------


class TestBackpressure:
    async def test_queuefull_increments_strike_counter(self) -> None:
        emitter = Emitter(max_queue_per_client=2)
        cid = await emitter.register(_make_mock_ws())

        # First two broadcasts fit; third overflows.
        await emitter.broadcast({"i": 1})
        await emitter.broadcast({"i": 2})
        assert emitter._consecutive_full[cid] == 0

        await emitter.broadcast({"i": 3})
        assert emitter._consecutive_full[cid] == 1

    async def test_successful_put_resets_strike_counter(self) -> None:
        emitter = Emitter(max_queue_per_client=1)
        cid = await emitter.register(_make_mock_ws())

        await emitter.broadcast({"i": 1})   # queue filled, strikes=0
        await emitter.broadcast({"i": 2})   # full → strikes=1
        assert emitter._consecutive_full[cid] == 1

        # Drain so next put_nowait succeeds.
        _, queue = emitter._clients[cid]
        _ = queue.get_nowait()

        await emitter.broadcast({"i": 3})   # success → strikes reset
        assert emitter._consecutive_full[cid] == 0

    async def test_three_strikes_triggers_disconnect(self) -> None:
        emitter = Emitter(max_queue_per_client=1)
        ws = _make_mock_ws()
        cid = await emitter.register(ws)

        await emitter.broadcast({"i": 0})   # fills queue
        for i in range(_BACKPRESSURE_STRIKES):
            await emitter.broadcast({"i": 100 + i})

        # Background disconnect task is scheduled; wait for it to run.
        await _wait_for(lambda: emitter.client_count == 0, timeout=1.0)

        ws.close.assert_awaited_once()
        kwargs = ws.close.await_args.kwargs
        assert kwargs.get("code") == 1011
        assert cid not in emitter._clients
        assert cid not in emitter._consecutive_full


# ---------------------------------------------------------------------------
# TestPeerIsolation
# ---------------------------------------------------------------------------


class TestPeerIsolation:
    async def test_other_clients_unaffected_by_disconnect(self) -> None:
        """A slow client hitting the strike threshold must not disturb peers."""
        emitter = Emitter(max_queue_per_client=1)
        slow_ws = _make_mock_ws()
        slow_cid = await emitter.register(slow_ws)
        fast_ws = _make_mock_ws()
        fast_cid = await emitter.register(fast_ws)

        # Iterate one fill broadcast + STRIKES more, draining fast each
        # iteration so its queue stays available.
        for i in range(_BACKPRESSURE_STRIKES + 1):
            await emitter.broadcast({"i": i})
            if fast_cid in emitter._clients:
                _, fast_q = emitter._clients[fast_cid]
                while not fast_q.empty():
                    fast_q.get_nowait()

        await _wait_for(lambda: slow_cid not in emitter._clients, timeout=1.0)

        slow_ws.close.assert_awaited_once()
        assert slow_cid not in emitter._clients
        fast_ws.close.assert_not_awaited()
        assert fast_cid in emitter._clients
