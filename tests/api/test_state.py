"""tests/api/test_state.py — Unit tests for api/state.BotState.

Pure unit tests: no asyncio dependency framework, no aiohttp, no
risk_manager import. All risk objects are MagicMock; real Pydantic
models (StrategyConfigPatch, StrategyRuntimeConfig) are used directly.
"""
from __future__ import annotations

import asyncio
from collections import deque
from unittest.mock import MagicMock

import pytest

from api.schemas import StrategyConfigPatch, StrategyRuntimeConfig
from api.state import BotState

# ---------------------------------------------------------------------------
# Group 1 — BotState.create()
# ---------------------------------------------------------------------------


def test_create_equity_fields() -> None:
    state = BotState.create(equity=5000.0, day_start_equity=4800.0)
    assert state.equity == 5000.0
    assert state.day_start_equity == 4800.0


def test_create_empty_collections() -> None:
    state = BotState.create(equity=5000.0, day_start_equity=4800.0)
    assert state.open_positions == {}
    assert state.ws_subscribers == []
    assert isinstance(state.recent_fills, deque)
    assert state.recent_fills.maxlen == 200


def test_create_strategy_configs_and_flags() -> None:
    state = BotState.create(equity=5000.0, day_start_equity=4800.0)
    assert set(state.strategy_configs.keys()) == {"strategy1", "strategy2", "strategy3"}
    assert all(cfg.enabled is True for cfg in state.strategy_configs.values())
    assert state.kill_switch_requested is False
    assert state.last_updated_at_ms == 0


# ---------------------------------------------------------------------------
# Group 2 — BotState.from_risk()
# ---------------------------------------------------------------------------


def test_from_risk_happy_path() -> None:
    risk = MagicMock()
    risk.equity = 10000.0
    risk._state.day_start_equity = 9500.0
    risk._state.open_positions = {"MKT-A:YES": object()}

    state = BotState.from_risk(risk, MagicMock())

    assert state.equity == 10000.0
    assert state.day_start_equity == 9500.0
    assert "MKT-A:YES" in state.open_positions
    assert set(state.strategy_configs.keys()) == {"strategy1", "strategy2", "strategy3"}
    assert all(cfg.enabled is True for cfg in state.strategy_configs.values())
    assert state.ws_subscribers == []


def test_from_risk_missing_state_fallback() -> None:
    # MagicMock(spec=["equity"]) means only 'equity' is a valid attribute;
    # getattr(risk, "_state", None) returns None for any attr not in spec.
    risk = MagicMock(spec=["equity"])
    risk.equity = 7777.0

    state = BotState.from_risk(risk, MagicMock())

    assert state.equity == 7777.0
    # When _state is None, from_risk falls back: day_start_equity == equity
    assert state.day_start_equity == 7777.0


def test_from_risk_zero_equity_edge_case() -> None:
    risk = MagicMock()
    risk.equity = 0.0
    risk._state.day_start_equity = 0.0
    risk._state.open_positions = {}

    state = BotState.from_risk(risk, MagicMock())

    assert state.equity == 0.0
    assert state.day_start_equity == 0.0
    assert state.open_positions == {}


# ---------------------------------------------------------------------------
# Group 3 — BotState.shallow_copy()
# ---------------------------------------------------------------------------


def test_shallow_copy_new_object() -> None:
    original = BotState.create(equity=10000.0, day_start_equity=9500.0)
    copy = original.shallow_copy()
    assert copy is not original


def test_shallow_copy_scalars_match() -> None:
    original = BotState.create(equity=10000.0, day_start_equity=9500.0)
    original.kill_switch_requested = True
    original.last_updated_at_ms = 999
    copy = original.shallow_copy()

    assert copy.equity == original.equity
    assert copy.day_start_equity == original.day_start_equity
    assert copy.kill_switch_requested == original.kill_switch_requested
    assert copy.last_updated_at_ms == original.last_updated_at_ms


def test_shallow_copy_collection_isolation() -> None:
    original = BotState.create(equity=10000.0, day_start_equity=9500.0)
    copy = original.shallow_copy()

    # Mutate original's open_positions — copy must be unaffected
    original.open_positions["new_key"] = "new_value"
    assert "new_key" not in copy.open_positions

    # Mutate original's strategy_configs — copy must be unaffected
    original.strategy_configs["strategy1"] = StrategyRuntimeConfig(
        id="strategy1", enabled=False
    )
    assert copy.strategy_configs["strategy1"].enabled is True

    # Mutate copy's open_positions — original must be unaffected
    copy.open_positions["copy_key"] = "copy_value"
    assert "copy_key" not in original.open_positions

    # Mutate copy's strategy_configs — original must be unaffected
    copy.strategy_configs["strategy2"] = StrategyRuntimeConfig(
        id="strategy2", maxPositionSize=50.0
    )
    assert original.strategy_configs["strategy2"].maxPositionSize == 100.0


def test_shallow_copy_ws_subscribers_empty() -> None:
    original = BotState.create(equity=10000.0, day_start_equity=9500.0)
    original.ws_subscribers.append(MagicMock())
    assert len(original.ws_subscribers) == 1

    copy = original.shallow_copy()
    assert copy.ws_subscribers == []


def test_shallow_copy_new_lock_and_preserved_maxlen() -> None:
    original = BotState.create(equity=10000.0, day_start_equity=9500.0)
    copy = original.shallow_copy()
    assert copy._write_lock is not original._write_lock
    assert copy.recent_fills.maxlen == 200


# ---------------------------------------------------------------------------
# Group 4 — BotState.apply_strategy_patch()
# ---------------------------------------------------------------------------


def test_apply_strategy_patch_fields_applied_defaults_preserved() -> None:
    state = BotState.create(equity=0.0, day_start_equity=0.0)
    patch = StrategyConfigPatch(id="strategy1", enabled=False, maxPositionSize=75.0)
    state.apply_strategy_patch(patch)

    cfg = state.strategy_configs["strategy1"]
    assert cfg.enabled is False
    assert cfg.maxPositionSize == 75.0
    assert cfg.riskBudget == 0.33      # default preserved
    assert cfg.activePairs is None     # default preserved


def test_apply_strategy_patch_returns_new_instance() -> None:
    state = BotState.create(equity=0.0, day_start_equity=0.0)
    original_cfg = state.strategy_configs["strategy1"]
    patch = StrategyConfigPatch(id="strategy1", enabled=False)
    state.apply_strategy_patch(patch)

    assert state.strategy_configs["strategy1"] is not original_cfg


def test_apply_strategy_patch_sibling_strategies_untouched() -> None:
    state = BotState.create(equity=0.0, day_start_equity=0.0)
    patch = StrategyConfigPatch(id="strategy2", enabled=False, maxPositionSize=50.0)
    state.apply_strategy_patch(patch)

    # strategy1 and strategy3 must be at defaults
    assert state.strategy_configs["strategy1"].enabled is True
    assert state.strategy_configs["strategy1"].maxPositionSize == 100.0
    assert state.strategy_configs["strategy3"].enabled is True
    assert state.strategy_configs["strategy3"].maxPositionSize == 100.0


# ---------------------------------------------------------------------------
# Group 5 — BotState.get_strategy_config()
# ---------------------------------------------------------------------------


def test_get_strategy_config_valid_id() -> None:
    state = BotState.create(equity=0.0, day_start_equity=0.0)
    cfg = state.get_strategy_config("strategy1")
    assert isinstance(cfg, StrategyRuntimeConfig)
    assert cfg.id == "strategy1"


def test_get_strategy_config_invalid_id_raises_key_error() -> None:
    state = BotState.create(equity=0.0, day_start_equity=0.0)
    with pytest.raises(KeyError):
        state.get_strategy_config("strategy99")


# ---------------------------------------------------------------------------
# Group 6 — write_lock property
# ---------------------------------------------------------------------------


def test_write_lock_identity() -> None:
    state = BotState.create(equity=0.0, day_start_equity=0.0)
    assert state.write_lock is state._write_lock


def test_write_lock_read_only() -> None:
    state = BotState.create(equity=0.0, day_start_equity=0.0)
    with pytest.raises(AttributeError):
        state.write_lock = asyncio.Lock()  # type: ignore[misc]


def test_write_lock_acquirable() -> None:
    state = BotState.create(equity=0.0, day_start_equity=0.0)

    async def _acquire() -> bool:
        async with state.write_lock:
            return True

    try:
        result = asyncio.run(_acquire())
        assert result is True
    except RuntimeError as exc:
        pytest.skip(f"asyncio.run not available in this environment: {exc}")
