"""tests/api/test_server.py — Tests for api/server.py run() coroutine.

Covers:
- Normal startup: api_server_started and api_server_stopped log events
- Graceful shutdown on asyncio.Event
- Task liveness before shutdown is set
- bind_host / port pass-through from config
- runner.cleanup() called on normal shutdown (finally block)
- Fatal error: RuntimeError from TCPSite.start re-raises and emits critical log
- runner.cleanup() called on fatal error (finally block)
"""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web

import api.server
from api.server import run


# ---------------------------------------------------------------------------
# File-level fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def fast_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    """Speed up the shutdown-event polling loop for all tests in this file."""
    monkeypatch.setattr(api.server, "_SHUTDOWN_POLL_INTERVAL_S", 0.01)


@pytest.fixture
def mock_logger(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace api.server.logger with a MagicMock for assertion."""
    m = MagicMock()
    monkeypatch.setattr(api.server, "logger", m)
    return m


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


async def _run_and_shutdown(
    state: Any,
    config: Any,
    delay: float = 0.05,
) -> None:
    """Spawn run() as a task, wait briefly, set shutdown, await completion."""
    shutdown = asyncio.Event()
    task = asyncio.ensure_future(run(state, config, shutdown))
    await asyncio.sleep(delay)
    shutdown.set()
    await asyncio.wait_for(task, timeout=2.0)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_server_starts_and_emits_started_log(
    bot_config: MagicMock,
    bot_state: Any,
    mock_logger: MagicMock,
) -> None:
    """run() logs api_server_started and api_server_stopped on normal path."""
    bot_config.http_api.port = 0
    await _run_and_shutdown(bot_state, bot_config)
    mock_logger.info.assert_any_call("api_server_started", host="127.0.0.1", port=0)
    mock_logger.info.assert_any_call("api_server_stopped")


@pytest.mark.asyncio
async def test_server_graceful_shutdown_on_event(
    bot_config: MagicMock,
    bot_state: Any,
    mock_logger: MagicMock,
) -> None:
    """Setting the shutdown Event causes run() to exit cleanly (no exception)."""
    bot_config.http_api.port = 0
    shutdown = asyncio.Event()
    task = asyncio.ensure_future(run(bot_state, bot_config, shutdown))
    await asyncio.sleep(0.05)
    assert task.done() is False
    shutdown.set()
    await asyncio.wait_for(task, timeout=2.0)
    # Task completed without raising
    assert task.exception() is None


@pytest.mark.asyncio
async def test_server_task_not_done_before_shutdown(
    bot_config: MagicMock,
    bot_state: Any,
    mock_logger: MagicMock,
) -> None:
    """run() blocks (task not done) until the shutdown event is set."""
    bot_config.http_api.port = 0
    shutdown = asyncio.Event()
    task = asyncio.ensure_future(run(bot_state, bot_config, shutdown))
    await asyncio.sleep(0.05)
    assert task.done() is False
    # Clean up
    shutdown.set()
    await asyncio.wait_for(task, timeout=2.0)


@pytest.mark.asyncio
async def test_server_uses_config_bind_host_and_port(
    bot_config: MagicMock,
    bot_state: Any,
    mock_logger: MagicMock,
) -> None:
    """run() reads bind_host and port from config.http_api and passes them to TCPSite."""
    bot_config.http_api.port = 0
    bot_config.http_api.bind_host = "127.0.0.1"
    await _run_and_shutdown(bot_state, bot_config)
    mock_logger.info.assert_any_call("api_server_started", host="127.0.0.1", port=0)


@pytest.mark.asyncio
async def test_server_cleanup_runs_on_normal_shutdown(
    bot_config: MagicMock,
    bot_state: Any,
    mock_logger: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """runner.cleanup() is called exactly once on graceful shutdown (finally block)."""
    cleanup_mock = AsyncMock()
    monkeypatch.setattr(web.AppRunner, "cleanup", cleanup_mock)
    bot_config.http_api.port = 0
    await _run_and_shutdown(bot_state, bot_config)
    cleanup_mock.assert_called_once()


@pytest.mark.asyncio
async def test_server_fatal_error_reraises_and_logs_critical(
    bot_config: MagicMock,
    bot_state: Any,
    mock_logger: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RuntimeError from TCPSite.start re-raises out of run() and triggers critical log."""
    monkeypatch.setattr(
        web.TCPSite,
        "start",
        AsyncMock(side_effect=RuntimeError("simulated boom")),
    )
    bot_config.http_api.port = 0
    shutdown = asyncio.Event()
    task = asyncio.ensure_future(run(bot_state, bot_config, shutdown))
    with pytest.raises(RuntimeError, match="simulated boom"):
        await asyncio.wait_for(task, timeout=2.0)
    mock_logger.critical.assert_called_once()
    call_args = mock_logger.critical.call_args
    assert call_args.args[0] == "api_server_fatal_error"
    assert call_args.kwargs.get("exc_info") is True


@pytest.mark.asyncio
async def test_server_cleanup_runs_on_fatal_error(
    bot_config: MagicMock,
    bot_state: Any,
    mock_logger: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """runner.cleanup() is called exactly once even when TCPSite.start raises (finally block)."""
    monkeypatch.setattr(
        web.TCPSite,
        "start",
        AsyncMock(side_effect=RuntimeError("boom")),
    )
    cleanup_mock = AsyncMock()
    monkeypatch.setattr(web.AppRunner, "cleanup", cleanup_mock)
    bot_config.http_api.port = 0
    shutdown = asyncio.Event()
    task = asyncio.ensure_future(run(bot_state, bot_config, shutdown))
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(task, timeout=2.0)
    cleanup_mock.assert_called_once()
