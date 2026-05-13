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
    return config


@pytest.fixture
def bot_state() -> BotState:
    """Real BotState with two sample fills pre-populated."""
    state = BotState.create(equity=10000.0, day_start_equity=9500.0)
    state.recent_fills.append(
        FillRecord(
            id="fill-001",
            marketId="MKT-BTC-001",
            outcome="YES",
            action="UP",
            quantity=10.0,
            price=0.65,
            strategy="strategy1",
            ts=1700000000000,
        )
    )
    state.recent_fills.append(
        FillRecord(
            id="fill-002",
            marketId="MKT-ETH-001",
            outcome="NO",
            action="DOWN",
            quantity=5.0,
            price=0.42,
            strategy="strategy2",
            ts=1700000060000,
        )
    )
    return state
