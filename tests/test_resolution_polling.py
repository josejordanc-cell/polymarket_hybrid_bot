"""Tests for REST resolution polling → RiskManager.on_settlement wiring."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import structlog

from config.settings import ApiConfig, BotConfig, RiskConfig, RuntimeConfig, StrategyConfig
from data_feeds.polymarket_feed import ResolutionEvent
from main import resolution_polling_loop
from risk_manager import RiskManager
from utils.logging import configure_logging
from utils.types import OrderIntent


def _make_config(**kwargs: object) -> BotConfig:
    return BotConfig(
        api=ApiConfig(),
        risk=RiskConfig(),
        strategy=StrategyConfig(),
        runtime=RuntimeConfig(),
        **kwargs,
    )


@pytest.mark.asyncio
async def test_resolution_poll_settles_yes_and_updates_equity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure_logging("CRITICAL")
    # Isolate BOT_DATA_DIR so PR2's persist_settlement hook in
    # resolution_polling_loop writes to tmp_path, not working-tree data/.
    monkeypatch.setenv("BOT_DATA_DIR", str(tmp_path))

    cfg = _make_config(resolution_poll_seconds=60.0)
    risk = RiskManager(cfg)
    mid = "0xcondition111"

    intent = OrderIntent(
        market_id=mid,
        token_id="yestok",
        side="BUY",
        outcome="YES",
        size=100.0,
        price=0.5,
        strategy="test",
    )
    risk.on_fill(intent, 0.5, 100.0)
    eq_after_fill = risk.equity

    async def fake_poll(mids: list[str]) -> list[ResolutionEvent]:
        assert mid in mids
        return [
            ResolutionEvent(
                mid,
                "YES",
                datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
            )
        ]

    monkeypatch.setattr(
        "data_feeds.polymarket_feed.poll_market_resolutions",
        fake_poll,
    )

    alerts: list[tuple[str, str]] = []

    async def capture_alert(msg: str, level: str = "info", **kwargs: object) -> None:
        alerts.append((msg, level))

    monkeypatch.setattr("utils.alerts.send_alert", capture_alert)

    shutdown = asyncio.Event()
    sleeps = 0

    async def finite_sleep(shutdown_ev: asyncio.Event, seconds: float) -> bool:
        nonlocal sleeps
        sleeps += 1
        if sleeps >= 2:
            shutdown_ev.set()
        return shutdown_ev.is_set()

    monkeypatch.setattr("main._interruptible_sleep", finite_sleep)

    await resolution_polling_loop(risk, cfg, shutdown)

    assert risk.open_positions == {}
    assert risk.equity > eq_after_fill
    assert risk.equity == pytest.approx(10_049.9, rel=1e-5)


@pytest.mark.asyncio
async def test_resolution_poll_five_failures_degraded_alert_and_metrics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure_logging("CRITICAL")
    # Isolate BOT_DATA_DIR for hermeticity consistency. This test's poll
    # always raises (no settlements processed → no persist_settlement call
    # → no write would happen even without the monkeypatch), but the
    # isolation pattern stays consistent across the file.
    monkeypatch.setenv("BOT_DATA_DIR", str(tmp_path))

    cfg = _make_config(resolution_poll_seconds=60.0)
    risk = RiskManager(cfg)
    intent = OrderIntent(
        market_id="0xfailmkt",
        token_id="tok",
        side="BUY",
        outcome="YES",
        size=10.0,
        price=0.4,
        strategy="test",
    )
    risk.on_fill(intent, 0.4, 10.0)

    async def failing_poll(mids: list[str]) -> list[ResolutionEvent]:
        raise ConnectionError("simulated network failure")

    monkeypatch.setattr(
        "data_feeds.polymarket_feed.poll_market_resolutions",
        failing_poll,
    )

    alerts: list[tuple[str, str]] = []

    async def capture_alert(msg: str, level: str = "info", **kwargs: object) -> None:
        alerts.append((msg, level))

    monkeypatch.setattr("utils.alerts.send_alert", capture_alert)

    mx = MagicMock()
    monkeypatch.setattr(
        "observability.metrics_exporter.get_metrics_exporter",
        lambda: mx,
    )

    log_events: list[tuple[str, dict[str, object]]] = []

    class FakeLog:
        def warning(self, event: str, **kw: object) -> None:
            log_events.append((event, kw))

        def error(self, event: str, **kw: object) -> None:
            log_events.append((event, kw))

        def info(self, event: str, **kw: object) -> None:
            log_events.append((event, kw))

    fake = FakeLog()
    monkeypatch.setattr(structlog, "get_logger", lambda *_args, **_kwargs: fake)

    shutdown = asyncio.Event()
    sleeps = 0

    async def finite_sleep(shutdown_ev: asyncio.Event, seconds: float) -> bool:
        nonlocal sleeps
        sleeps += 1
        if sleeps >= 9:
            shutdown_ev.set()
        return shutdown_ev.is_set()

    monkeypatch.setattr("main._interruptible_sleep", finite_sleep)

    await resolution_polling_loop(risk, cfg, shutdown)

    degraded = [e for e, _ in log_events if e == "resolution_polling_degraded"]
    assert len(degraded) >= 1

    crit_alerts = [a for a in alerts if a[1] == "critical"]
    assert len(crit_alerts) >= 1
    assert "degraded" in crit_alerts[0][0].lower() or "failures" in crit_alerts[0][0].lower()

    mx.record_resolution_poll_failure.assert_called()
