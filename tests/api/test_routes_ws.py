"""tests/api/test_routes_ws.py — Integration tests for api/routes_ws.py.

Covers ws_handler end-to-end via aiohttp TestClient + ws_connect:

    TestWsAuth     — five 4001-close cases (missing / bad-sig / expired
                     / replayed / wrong-alg)                       5 tests
    TestWsLifecycle— valid upgrade, broadcast delivery, disconnect
                     cleanup                                       3 tests

Total: 8 tests.

asyncio_mode=auto is set in pytest.ini so async def tests need no
decorator. WS close frames are observed by reading WSMessage.type ==
WSMsgType.CLOSE; the close code arrives in WSMessage.data.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid

import jwt
import pytest
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer

from api.emit import Emitter
from api.routes_ws import ws_handler


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ticket(
    secret: str,
    *,
    sub: str | None = None,
    exp: int | None = None,
    algorithm: str = "HS256",
    include_iat: bool = True,
) -> str:
    """Build a JWT for ws_handler tests. Fresh sub UUID per call by default."""
    now = int(time.time())
    payload: dict = {"sub": sub if sub is not None else f"ws-test-{uuid.uuid4()}"}
    if include_iat:
        payload["iat"] = now
    payload["exp"] = exp if exp is not None else now + 60
    return jwt.encode(payload, secret, algorithm=algorithm)


async def _wait_for(predicate, *, timeout: float = 1.0, interval: float = 0.01) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return False


# ---------------------------------------------------------------------------
# Fixture: aiohttp TestClient with /ws wired through ws_handler
# ---------------------------------------------------------------------------


@pytest.fixture
async def ws_test_client(bot_config):
    """Yield (client, emitter) — emitter is the real one wired into app."""
    emitter = Emitter()
    app = web.Application()
    app["emitter"] = emitter
    app["config"] = bot_config
    app.router.add_get("/ws", ws_handler)

    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        yield client, emitter
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# TestWsAuth — five 4001 close paths
# ---------------------------------------------------------------------------


class TestWsAuth:
    async def test_missing_ticket_closes_4001(self, ws_test_client) -> None:
        client, emitter = ws_test_client
        async with client.ws_connect("/ws") as ws:
            msg = await ws.receive(timeout=2.0)
            assert msg.type == WSMsgType.CLOSE
            assert msg.data == 4001
        assert emitter.client_count == 0

    async def test_bad_signature_closes_4001(self, ws_test_client) -> None:
        client, emitter = ws_test_client
        token = _make_ticket("wrong-secret-entirely")
        async with client.ws_connect(f"/ws?ticket={token}") as ws:
            msg = await ws.receive(timeout=2.0)
            assert msg.type == WSMsgType.CLOSE
            assert msg.data == 4001
        assert emitter.client_count == 0

    async def test_expired_ticket_closes_4001(
        self, ws_test_client, bot_config
    ) -> None:
        client, emitter = ws_test_client
        secret = bot_config.api.ws_ticket_secret.get_secret_value()
        token = _make_ticket(secret, exp=int(time.time()) - 10)
        async with client.ws_connect(f"/ws?ticket={token}") as ws:
            msg = await ws.receive(timeout=2.0)
            assert msg.type == WSMsgType.CLOSE
            assert msg.data == 4001
        assert emitter.client_count == 0

    async def test_replayed_ticket_closes_4001(
        self, ws_test_client, bot_config
    ) -> None:
        client, emitter = ws_test_client
        secret = bot_config.api.ws_ticket_secret.get_secret_value()
        pinned_sub = f"ws-replay-{uuid.uuid4()}"
        token = _make_ticket(secret, sub=pinned_sub)

        # First use: must upgrade successfully.
        async with client.ws_connect(f"/ws?ticket={token}") as ws:
            await _wait_for(lambda: emitter.client_count == 1, timeout=1.0)
            await ws.close()

        # Allow handler finally block to run + unregister.
        await _wait_for(lambda: emitter.client_count == 0, timeout=1.0)

        # Replay: same ticket → 4001.
        async with client.ws_connect(f"/ws?ticket={token}") as ws:
            msg = await ws.receive(timeout=2.0)
            assert msg.type == WSMsgType.CLOSE
            assert msg.data == 4001
        assert emitter.client_count == 0

    async def test_wrong_algorithm_closes_4001(
        self, ws_test_client, bot_config
    ) -> None:
        client, emitter = ws_test_client
        secret = bot_config.api.ws_ticket_secret.get_secret_value()
        token = _make_ticket(secret, algorithm="HS512")
        async with client.ws_connect(f"/ws?ticket={token}") as ws:
            msg = await ws.receive(timeout=2.0)
            assert msg.type == WSMsgType.CLOSE
            assert msg.data == 4001
        assert emitter.client_count == 0


# ---------------------------------------------------------------------------
# TestWsLifecycle — happy path
# ---------------------------------------------------------------------------


class TestWsLifecycle:
    async def test_valid_ticket_upgrades_and_registers(
        self, ws_test_client, bot_config
    ) -> None:
        client, emitter = ws_test_client
        secret = bot_config.api.ws_ticket_secret.get_secret_value()
        token = _make_ticket(secret)
        async with client.ws_connect(f"/ws?ticket={token}") as ws:
            assert await _wait_for(
                lambda: emitter.client_count == 1, timeout=2.0
            )
            await ws.close()
        assert await _wait_for(
            lambda: emitter.client_count == 0, timeout=2.0
        )

    async def test_broadcast_delivers_after_upgrade(
        self, ws_test_client, bot_config
    ) -> None:
        client, emitter = ws_test_client
        secret = bot_config.api.ws_ticket_secret.get_secret_value()
        token = _make_ticket(secret)
        async with client.ws_connect(f"/ws?ticket={token}") as ws:
            assert await _wait_for(
                lambda: emitter.client_count == 1, timeout=2.0
            )

            msg = {"type": "TICK", "payload": {"marketId": "MKT-X", "price": 0.5}}
            await emitter.broadcast(msg)

            received = await ws.receive(timeout=2.0)
            assert received.type == WSMsgType.TEXT
            assert json.loads(received.data) == msg
            await ws.close()

    async def test_client_disconnect_unregisters(
        self, ws_test_client, bot_config
    ) -> None:
        client, emitter = ws_test_client
        secret = bot_config.api.ws_ticket_secret.get_secret_value()
        token = _make_ticket(secret)
        async with client.ws_connect(f"/ws?ticket={token}") as ws:
            assert await _wait_for(
                lambda: emitter.client_count == 1, timeout=2.0
            )
            await ws.close()

        # Handler finally block must run; client_count returns to 0.
        assert await _wait_for(
            lambda: emitter.client_count == 0, timeout=2.0
        )
