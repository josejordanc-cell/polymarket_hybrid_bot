"""tests/utils/test_logs.py — v0.2.9 B.8 log-feed wiring tests.

Covers utils/logs.py:
  - _map_log_level (the 3f Option C 5→4-tier lossy map, moved from
    api/routes_rest.py to its natural home in utils/logs.py)
  - log_to_dashboard (the public B.8 wiring helper)

Mirrors tests/utils/test_alerts.py patterns: autouse state-reset
fixture, AsyncMock-friendly setup, ordering invariants.
"""
from __future__ import annotations

import pytest

from api.state import BotState, set_state
from utils.logs import _LOG_LEVEL_MAP, _map_log_level, log_to_dashboard


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_state():
    """Clear the module-level BotState before and after every test."""
    set_state(None)
    yield
    set_state(None)


def _make_bot_state() -> BotState:
    """Minimal BotState (no fills, no positions) for log-write tests."""
    return BotState.create(equity=10000.0, day_start_equity=9500.0)


# ---------------------------------------------------------------------------
# TestMapLogLevel — moved here from tests/api/test_routes_rest.py
# alongside the helper's new module.
# ---------------------------------------------------------------------------


class TestMapLogLevel:
    """Internal Python-logging level → wire BotLogEntry.level map.

    Locked policy (Option C, Jose 2026-05-21):
      debug    → None (drop entry)
      info     → "INFO"
      warning  → "WARN"
      error    → "ERROR"
      critical → "ERROR"   (lossy collapse to highest wire tier)

    Bot NEVER emits "EXEC" — vestigial slot in the wire enum.
    """

    def test_debug_drops(self) -> None:
        assert _map_log_level("debug") is None

    def test_info_maps_to_INFO(self) -> None:
        assert _map_log_level("info") == "INFO"

    def test_warning_maps_to_WARN(self) -> None:
        assert _map_log_level("warning") == "WARN"

    def test_error_maps_to_ERROR(self) -> None:
        assert _map_log_level("error") == "ERROR"

    def test_critical_collapses_to_ERROR(self) -> None:
        """Lossy collapse: critical → ERROR (no higher wire tier)."""
        assert _map_log_level("critical") == "ERROR"

    def test_unknown_raises(self) -> None:
        """Unknown internal levels raise rather than silently corrupt the
        wire enum. Mirrors _collapse_position_id's defensive policy."""
        with pytest.raises(ValueError):
            _map_log_level("trace")
        with pytest.raises(ValueError):
            _map_log_level("INFO")  # uppercase is wire-side, not internal
        with pytest.raises(ValueError):
            _map_log_level("")

    def test_map_contains_exactly_the_5_internal_tiers(self) -> None:
        """Regression guard against silently adding tiers — any future
        change should flow through a policy decision."""
        assert set(_LOG_LEVEL_MAP.keys()) == {
            "debug",
            "info",
            "warning",
            "error",
            "critical",
        }


# ---------------------------------------------------------------------------
# TestLogToDashboard — B.8 wiring
# ---------------------------------------------------------------------------


class TestLogToDashboard:
    """log_to_dashboard appends a wire-shape BotLogEntry to
    BotState.recent_logs (state-only; no WS broadcast because Zod has
    no LOG variant). 3f Option C map applied at the boundary."""

    @pytest.mark.asyncio
    async def test_info_appends_with_INFO_wire_level(self) -> None:
        from api.schemas import BotLogEntry

        state = _make_bot_state()
        set_state(state)

        await log_to_dashboard("hello world", level="info", source="unit")

        assert len(state.recent_logs) == 1
        entry = state.recent_logs[0]
        assert isinstance(entry, BotLogEntry)
        assert entry.level == "INFO"
        # source folded into message (BotLogEntry has no source field).
        assert entry.message == "[unit] hello world"

    @pytest.mark.asyncio
    async def test_warning_maps_to_WARN(self) -> None:
        state = _make_bot_state()
        set_state(state)
        await log_to_dashboard("watch out", level="warning")
        assert state.recent_logs[0].level == "WARN"

    @pytest.mark.asyncio
    async def test_error_maps_to_ERROR(self) -> None:
        state = _make_bot_state()
        set_state(state)
        await log_to_dashboard("something broke", level="error")
        assert state.recent_logs[0].level == "ERROR"

    @pytest.mark.asyncio
    async def test_critical_collapses_to_ERROR(self) -> None:
        """3f Option C: critical → ERROR (no higher wire tier)."""
        state = _make_bot_state()
        set_state(state)
        await log_to_dashboard("the bad thing", level="critical")
        assert state.recent_logs[0].level == "ERROR"

    @pytest.mark.asyncio
    async def test_debug_silently_dropped(self) -> None:
        """3f Option C: debug entries never reach the dashboard. The
        call must NOT raise and MUST NOT touch recent_logs."""
        state = _make_bot_state()
        set_state(state)
        await log_to_dashboard("verbose noise", level="debug")
        assert len(state.recent_logs) == 0

    @pytest.mark.asyncio
    async def test_no_op_when_state_unregistered(self) -> None:
        """Headless / unit-test environment: with no BotState
        registered, log_to_dashboard silently no-ops. Must not raise."""
        # set_state(None) by autouse fixture.
        await log_to_dashboard("orphan log", level="info")
        # Nothing to assert except that the call completed cleanly.

    @pytest.mark.asyncio
    async def test_default_level_is_info(self) -> None:
        state = _make_bot_state()
        set_state(state)
        await log_to_dashboard("no level kwarg")
        assert state.recent_logs[0].level == "INFO"

    @pytest.mark.asyncio
    async def test_default_source_is_bot(self) -> None:
        state = _make_bot_state()
        set_state(state)
        await log_to_dashboard("no source kwarg", level="info")
        assert state.recent_logs[0].message == "[bot] no source kwarg"

    @pytest.mark.asyncio
    async def test_recent_logs_deque_bounded_at_maxlen(self) -> None:
        """BotState.recent_logs is declared deque(maxlen=500). Excess
        appends drop the oldest — verify end-to-end through
        log_to_dashboard."""
        state = _make_bot_state()
        set_state(state)
        maxlen = state.recent_logs.maxlen
        assert maxlen == 500

        for i in range(maxlen + 1):
            await log_to_dashboard(f"log-{i}", level="info")

        assert len(state.recent_logs) == maxlen
        messages = [e.message for e in state.recent_logs]
        # Oldest dropped; newest preserved.
        assert "[bot] log-0" not in messages
        assert messages[-1] == f"[bot] log-{maxlen}"

    @pytest.mark.asyncio
    async def test_unknown_level_swallowed_not_raised(self) -> None:
        """log_to_dashboard's fire-and-forget contract: any internal
        error (including a Pydantic validation failure or an unknown
        level) is logged and swallowed, never raised. The caller's
        flow must continue regardless."""
        state = _make_bot_state()
        set_state(state)

        # "trace" is not in _LOG_LEVEL_MAP → _map_log_level raises
        # ValueError internally; the outer try/except in
        # log_to_dashboard swallows it.
        await log_to_dashboard("unknown level", level="trace")  # type: ignore[arg-type]

        # Nothing appended.
        assert len(state.recent_logs) == 0

    @pytest.mark.asyncio
    async def test_id_unique_per_call(self) -> None:
        state = _make_bot_state()
        set_state(state)
        await log_to_dashboard("one")
        await log_to_dashboard("two")
        ids = [e.id for e in state.recent_logs]
        assert len(ids) == 2
        assert ids[0] != ids[1]

    @pytest.mark.asyncio
    async def test_emitted_entry_validates_against_pydantic_botlogentry(
        self,
    ) -> None:
        """Round-trip the appended entry through the bot's BotLogEntry
        schema to catch shape drift between log_to_dashboard and
        api.schemas.BotLogEntry. (Identical guard pattern to
        tests/utils/test_alerts.py's AlertItem round-trip.)"""
        from api.schemas import BotLogEntry

        state = _make_bot_state()
        set_state(state)
        await log_to_dashboard("validate me", level="warning", source="bot")

        entry = state.recent_logs[0]
        # If BotLogEntry fields drift again, this raises ValidationError.
        BotLogEntry.model_validate(entry.model_dump())
