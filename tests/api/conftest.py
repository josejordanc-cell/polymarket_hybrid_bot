"""tests/api/conftest.py — pytest fixtures for the API test suite.

Per SESSION-BRIEF §6: mock-only. No real RiskManager. BotConfig is a
MagicMock; BotState is the real dataclass (it has no external deps).
"""
from __future__ import annotations

from collections import deque
from unittest.mock import MagicMock

import pytest

from api.schemas import FillRecord
from api.state import BotState


@pytest.fixture
def bot_config() -> MagicMock:
    """MagicMock mimicking BotConfig with a nested .api block.

    Mirrors the surface api/auth.py and api/server.py read via getattr:
        config.http_api.bot_api_token.get_secret_value() -> str
        config.http_api.ws_ticket_secret.get_secret_value() -> str
        config.http_api.port -> int
        config.http_api.bind_host -> str
    """
    config = MagicMock()
    config.http_api.bot_api_token.get_secret_value.return_value = "test-bearer-token-abc123"
    config.http_api.ws_ticket_secret.get_secret_value.return_value = "test-ws-secret-xyz789"
    config.http_api.port = 8080
    config.http_api.bind_host = "127.0.0.1"
    # v0.2.9 3h-fix: the sim-mode-only debug endpoints
    # (POST /debug/inject-position, /debug/clear-positions) read the
    # mode field at ``config.runtime.mode`` per RuntimeConfig
    # (config/settings.py:219-236), NOT ``config.mode``. The earlier
    # ``config.mode = "sim"`` worked only because MagicMock auto-creates
    # any attribute on assignment — production BotConfig has no
    # top-level ``mode`` attribute. Default the nested path to "sim" so
    # the debug routes are reachable in tests; per-test override to
    # ``bot_config.runtime.mode = "live"`` for the 404-gate tests.
    config.runtime.mode = "sim"
    # v0.2.9 B.1: _build_riskmetrics reads
    # ``config.risk.daily_drawdown_limit_pct`` for the liquidationRisk
    # computation (lock 11). Without this explicit set, MagicMock's
    # auto-attribute creation returns a MagicMock instance which is
    # neither None nor a float — the helper raises TypeError on the
    # threshold comparison. Setting the production-realistic default
    # (0.12 = 12% per config/settings.py:161) makes the fixture match
    # real-config behavior. Per-test override is fine.
    config.risk.daily_drawdown_limit_pct = 0.12
    return config


@pytest.fixture
def bot_state() -> BotState:
    """Real BotState with two sample fills pre-populated."""
    state = BotState.create(equity=10000.0, day_start_equity=9500.0)
    state.recent_fills.append(
        FillRecord(
            id="fill-001",
            time="16:00:00",
            symbol="MKT-BTC-001",
            action="UP",
            price=0.65,
            size=10.0,
            strategy="strategy1",
        )
    )
    state.recent_fills.append(
        FillRecord(
            id="fill-002",
            time="16:01:00",
            symbol="MKT-ETH-001",
            action="DOWN",
            price=0.42,
            size=5.0,
            strategy="strategy2",
        )
    )
    return state
