"""
tests/utils/test_alerts.py — D.2.4 step 1 ALERT broadcast wire-up tests.

Verifies that utils.alerts.send_alert broadcasts a flat AlertItem-shaped
payload through the registered Emitter, per the contract reconciled with
the dashboard zod schema in this batch.

See docs/audit/d24-emit-site-audit.md §5.2 (gate) and §10 (drift
discovery + reconciliation) for context.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from api.emit import Emitter
from utils import alerts as alerts_mod
from utils.alerts import send_alert, set_emitter


@pytest.fixture(autouse=True)
def _reset_emitter():
    """Clear the module-level emitter before and after every test."""
    set_emitter(None)
    yield
    set_emitter(None)


def _make_mock_emitter() -> MagicMock:
    em = MagicMock(spec=Emitter)
    em.broadcast = AsyncMock()
    return em


@pytest.mark.asyncio
async def test_send_alert_broadcasts_when_emitter_registered() -> None:
    em = _make_mock_emitter()
    set_emitter(em)

    await send_alert("test message", level="warning", source="unit-test")

    assert em.broadcast.await_count == 1
    msg = em.broadcast.await_args.args[0]
    assert msg["type"] == "ALERT"
    payload = msg["payload"]
    assert set(payload.keys()) == {"id", "time", "severity", "source", "message"}
    assert payload["severity"] == "warning"
    assert payload["source"] == "unit-test"
    assert payload["message"] == "test message"
    assert isinstance(payload["id"], str) and payload["id"]
    assert isinstance(payload["time"], str) and payload["time"]


@pytest.mark.asyncio
async def test_send_alert_is_noop_broadcast_when_no_emitter() -> None:
    # No set_emitter() call; _reset_emitter fixture ensures it is None.
    assert alerts_mod._emitter is None
    # Must not raise.
    await send_alert("ignored", level="info")


@pytest.mark.parametrize("level", ["info", "warning", "critical"])
@pytest.mark.asyncio
async def test_send_alert_severity_round_trip(level: str) -> None:
    em = _make_mock_emitter()
    set_emitter(em)

    await send_alert("sev check", level=level)  # type: ignore[arg-type]

    msg = em.broadcast.await_args.args[0]
    assert msg["payload"]["severity"] == level


@pytest.mark.asyncio
async def test_send_alert_mints_unique_ids_across_calls() -> None:
    em = _make_mock_emitter()
    set_emitter(em)

    await send_alert("first")
    await send_alert("second")

    ids = [call.args[0]["payload"]["id"] for call in em.broadcast.await_args_list]
    assert len(ids) == 2
    assert ids[0] != ids[1]
    assert all(isinstance(i, str) and i for i in ids)


@pytest.mark.asyncio
async def test_send_alert_payload_validates_against_pydantic_alertitem() -> None:
    """Round-trip the emitted payload through the bot's AlertItem schema to
    catch shape drift between utils.alerts.send_alert and api.schemas.AlertItem.
    This is the assertion the D.2.3 test suite was missing."""
    from api.schemas import AlertItem

    em = _make_mock_emitter()
    set_emitter(em)

    await send_alert("validate me", level="critical", source="bot")

    payload = em.broadcast.await_args.args[0]["payload"]
    # If AlertItem fields drift again, this raises ValidationError.
    AlertItem.model_validate(payload)


# ---------------------------------------------------------------------------
# v0.2.9 B.9 — BotState.recent_alerts wiring tests
# ---------------------------------------------------------------------------


from api.state import BotState, set_state


@pytest.fixture(autouse=True)
def _reset_state():
    """Clear the module-level BotState before and after every test in
    this module — mirrors the _reset_emitter fixture pattern."""
    set_state(None)
    yield
    set_state(None)


def _make_bot_state() -> BotState:
    """Minimal BotState for alert-write tests (no fills, no positions)."""
    return BotState.create(equity=10000.0, day_start_equity=9500.0)


@pytest.mark.asyncio
async def test_send_alert_appends_to_botstate_recent_alerts() -> None:
    """v0.2.9 B.9: send_alert must atomically append a wire-shape
    AlertItem to BotState.recent_alerts. The append happens BEFORE the
    WS broadcast so reconnecting snapshot clients see the alert."""
    from api.schemas import AlertItem

    state = _make_bot_state()
    set_state(state)
    em = _make_mock_emitter()
    set_emitter(em)

    await send_alert("disk space low", level="warning", source="health")

    # State-side: exactly one AlertItem on the deque.
    assert len(state.recent_alerts) == 1
    appended = state.recent_alerts[0]
    assert isinstance(appended, AlertItem)
    assert appended.severity == "warning"
    assert appended.source == "health"
    assert appended.message == "disk space low"

    # Wire-side: broadcast saw the SAME shape (single source of truth).
    payload = em.broadcast.await_args.args[0]["payload"]
    assert payload["id"] == appended.id
    assert payload["severity"] == appended.severity
    assert payload["message"] == appended.message


@pytest.mark.asyncio
async def test_send_alert_state_write_no_op_when_state_unregistered() -> None:
    """When no BotState is registered, the state-write path silently
    no-ops and the WS broadcast still fires — mirrors the no-Emitter
    no-op pattern from D.2.4."""
    em = _make_mock_emitter()
    set_emitter(em)
    # set_state(None) by autouse fixture.

    await send_alert("no state", level="info")

    assert em.broadcast.await_count == 1


@pytest.mark.asyncio
async def test_send_alert_broadcast_no_op_when_emitter_unregistered() -> None:
    """When no Emitter is registered but a BotState is, the state-write
    still happens (snapshot history preserved). Inverse of the prior
    test."""
    state = _make_bot_state()
    set_state(state)
    # set_emitter(None) by autouse fixture.

    await send_alert("only state", level="info")

    assert len(state.recent_alerts) == 1


@pytest.mark.asyncio
async def test_recent_alerts_deque_bounded_at_maxlen() -> None:
    """BotState.recent_alerts is declared as deque(maxlen=200).
    Appending maxlen+1 alerts drops the oldest — verify the bounded
    behavior holds end-to-end through send_alert."""
    state = _make_bot_state()
    set_state(state)
    maxlen = state.recent_alerts.maxlen
    assert maxlen == 200  # documents the contract from api/state.py

    # Append maxlen+1 alerts.
    for i in range(maxlen + 1):
        await send_alert(f"alert-{i}", level="info")

    # Deque is exactly maxlen long; the oldest ("alert-0") is gone;
    # the newest ("alert-200") is the last entry.
    assert len(state.recent_alerts) == maxlen
    messages = [a.message for a in state.recent_alerts]
    assert "alert-0" not in messages
    assert messages[-1] == f"alert-{maxlen}"


@pytest.mark.asyncio
async def test_send_alert_state_write_before_broadcast() -> None:
    """v0.2.9 B.9 contract: state-write happens BEFORE the broadcast so
    a reconnecting client doing GET /dashboard/snapshot sees the alert
    even if the broadcast hasn't propagated yet. Verify ordering by
    using a broadcast side-effect that inspects state at WS dispatch
    time."""
    state = _make_bot_state()
    set_state(state)

    observed_state_lengths: list[int] = []

    em = MagicMock(spec=Emitter)

    async def _capture_state_at_broadcast(msg):
        # When the broadcast fires, state.recent_alerts MUST already
        # contain the new alert.
        observed_state_lengths.append(len(state.recent_alerts))

    em.broadcast = AsyncMock(side_effect=_capture_state_at_broadcast)
    set_emitter(em)

    await send_alert("ordered", level="info")

    assert observed_state_lengths == [1]
