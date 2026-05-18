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
