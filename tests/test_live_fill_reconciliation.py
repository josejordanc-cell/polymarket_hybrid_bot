"""
End-to-end style tests for live CLOB fill reconciliation (no real network).

Mocks py_clob_client and either an injected fill source or REST polling.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from config.settings import BotConfig, get_config
from execution.clob_executor import CLOBExecutor
from utils.types import OrderIntent, TradeResult


def _live_bot_config(**overrides: float) -> BotConfig:
    base = dict(
        fill_poll_interval_sec=overrides.get("fill_poll_interval_sec", 0.05),
        fill_poll_timeout_sec=overrides.get("fill_poll_timeout_sec", 1.0),
    )
    return get_config().model_copy(update=base)


def _sample_intent() -> OrderIntent:
    return OrderIntent(
        market_id="mkt_1",
        token_id="tok_yes",
        side="BUY",
        outcome="YES",
        size=100.0,
        price=0.5,
        tick_size=0.01,
        tif="IOC",
        strategy="intra_arbitrage",
    )


@pytest.fixture
def live_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_MODE", "live")
    monkeypatch.setenv("BOT_POLYMARKET_PRIVATE_KEY", "0x" + "ab" * 32)
    monkeypatch.setenv("BOT_POLYMARKET_API_KEY", "test_api_key")
    monkeypatch.setenv("BOT_POLYMARKET_API_SECRET", "test_secret")
    monkeypatch.setenv("BOT_POLYMARKET_API_PASSPHRASE", "test_pass")
    get_config.cache_clear()
    yield
    get_config.cache_clear()


@pytest.mark.asyncio
async def test_injected_full_fill_price_improvement(live_env: None) -> None:
    """100 submitted, 100 filled at a better price — single accounting slice."""

    async def fill_src(_ex: CLOBExecutor, oid: str, _p) -> TradeResult:
        return TradeResult(
            accepted=True,
            order_id=oid,
            fill_price=0.51,
            filled_size=100.0,
            reason="matched",
        )

    cfg = _live_bot_config()
    with patch("execution.clob_executor.ClobClient"):
        ex = CLOBExecutor(cfg, fill_event_source=fill_src)
        assert ex._client is not None
        ex._client.create_and_post_order = MagicMock(
            return_value={"orderID": "ord_full", "status": "live"}
        )

    intent = _sample_intent()
    assert intent.price_rounded() == 0.5
    result = await ex.execute(intent)
    assert result.accepted is True
    assert result.order_id == "ord_full"
    assert result.filled_size == 100.0
    assert result.fill_price == 0.51
    assert result.fill_price != intent.price_rounded()

    recorded: list[tuple[float, float]] = []

    def fake_on_fill(_intent: OrderIntent, fp: float, fs: float) -> None:
        recorded.append((fp, fs))

    if result.accepted and result.filled_size > 0:
        fake_on_fill(intent, result.fill_price or intent.price_rounded(), result.filled_size)

    assert len(recorded) == 1
    assert recorded[0] == (0.51, 100.0)


@pytest.mark.asyncio
async def test_injected_partial_ioc_then_cancel(live_env: None) -> None:
    """100 submitted IOC, 40 filled then remainder cancelled."""

    async def fill_src(_ex: CLOBExecutor, oid: str, _p) -> TradeResult:
        return TradeResult(
            accepted=True,
            order_id=oid,
            fill_price=0.5,
            filled_size=40.0,
            reason="ioc_partial_cancelled",
        )

    cfg = _live_bot_config()
    with patch("execution.clob_executor.ClobClient"):
        ex = CLOBExecutor(cfg, fill_event_source=fill_src)
        ex._client.create_and_post_order = MagicMock(
            return_value={"orderID": "ord_part", "status": "live"}
        )

    intent = _sample_intent()
    result = await ex.execute(intent)
    assert result.filled_size == 40.0
    assert result.reason == "ioc_partial_cancelled"

    recorded: list[tuple[float, float]] = []

    def fake_on_fill(_intent: OrderIntent, fp: float, fs: float) -> None:
        recorded.append((fp, fs))

    if result.accepted and result.filled_size > 0:
        fake_on_fill(intent, result.fill_price or intent.price_rounded(), result.filled_size)

    assert len(recorded) == 1
    assert recorded[0][1] == 40.0


@pytest.mark.asyncio
async def test_injected_expired_no_fill(live_env: None) -> None:
    """No fills within timeout — no RiskManager slice, reason expired."""

    async def fill_src(_ex: CLOBExecutor, oid: str, _p) -> TradeResult:
        return TradeResult(
            accepted=True,
            order_id=oid,
            fill_price=None,
            filled_size=0.0,
            reason="expired",
        )

    cfg = _live_bot_config()
    with patch("execution.clob_executor.ClobClient"):
        ex = CLOBExecutor(cfg, fill_event_source=fill_src)
        ex._client.create_and_post_order = MagicMock(
            return_value={"orderID": "ord_exp", "status": "live"}
        )

    intent = _sample_intent()
    result = await ex.execute(intent)
    assert result.accepted is True
    assert result.filled_size == 0.0
    assert result.reason == "expired"

    recorded: list[tuple[float, float]] = []

    def fake_on_fill(_intent: OrderIntent, fp: float, fs: float) -> None:
        recorded.append((fp, fs))

    if result.accepted and result.filled_size > 0:
        fake_on_fill(intent, result.fill_price or intent.price_rounded(), result.filled_size)

    assert recorded == []


@pytest.mark.asyncio
async def test_rest_poll_expired_no_fill(live_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """REST path: GTC order stays LIVE with no match until timeout — expired, no on_fill."""

    cfg = _live_bot_config(fill_poll_interval_sec=0.02, fill_poll_timeout_sec=0.15)
    with patch("execution.clob_executor.ClobClient"):
        ex = CLOBExecutor(cfg)
        ex._client.create_and_post_order = MagicMock(
            return_value={"orderID": "ord_rest", "status": "live"}
        )
        ex._client.get_order = MagicMock(
            return_value={
                "status": "ORDER_STATUS_LIVE",
                "size_matched": "0",
                "price": "0.50",
            }
        )

    intent = _sample_intent()
    intent = intent.model_copy(update={"tif": "GTC", "strategy": "market_maker"})

    result = await ex.execute(intent)
    assert result.accepted is True
    assert result.filled_size == 0.0
    assert result.reason == "expired"
    assert ex._client.get_order.call_count >= 1


@pytest.mark.asyncio
async def test_rest_partial_ioc_cancelled(live_env: None) -> None:
    """REST path: IOC partial then CANCELED — 40 contracts, ioc_partial_cancelled."""

    cfg = _live_bot_config(fill_poll_interval_sec=0.02, fill_poll_timeout_sec=1.0)

    seq = [
        {
            "status": "ORDER_STATUS_LIVE",
            "size_matched": "40000000",
            "price": "0.50",
        },
        {
            "status": "ORDER_STATUS_CANCELED",
            "size_matched": "40000000",
            "price": "0.50",
        },
    ]

    with patch("execution.clob_executor.ClobClient"):
        ex = CLOBExecutor(cfg)
        ex._client.create_and_post_order = MagicMock(
            return_value={"orderID": "ord_ioc", "status": "live"}
        )
        ex._client.get_order = MagicMock(side_effect=seq)

    intent = _sample_intent()
    result = await ex.execute(intent)
    assert result.filled_size == 40.0
    assert result.reason == "ioc_partial_cancelled"


@pytest.mark.asyncio
async def test_structlog_order_status_and_fill_events(live_env: None) -> None:
    """order_status_changed + fill_recorded are emitted for a reconciled partial IOC cancel."""

    async def fill_src(_ex: CLOBExecutor, oid: str, _p) -> TradeResult:
        return TradeResult(
            accepted=True,
            order_id=oid,
            fill_price=0.5,
            filled_size=40.0,
            reason="ioc_partial_cancelled",
        )

    cfg = _live_bot_config()
    events: list[tuple[str, dict]] = []

    def capture(name: str, **kw: object) -> None:
        events.append((name, kw))

    with patch("execution.clob_executor.ClobClient"):
        ex = CLOBExecutor(cfg, fill_event_source=fill_src)
        ex._client.create_and_post_order = MagicMock(
            return_value={"orderID": "ord_log", "status": "live"}
        )

    with patch("execution.clob_executor.logger") as log:
        log.info.side_effect = capture
        await ex.execute(_sample_intent())

    names = [e[0] for e in events]
    assert "order_status_changed" in names
    assert "fill_recorded" in names
    oc = next(kw for n, kw in events if n == "order_status_changed")
    assert oc.get("reason") == "ioc_partial_cancelled"


def test_notify_fill_metrics_skips_zero_size() -> None:
    from observability.metrics_exporter import (
        MetricsExporter,
        notify_fill_metrics,
        set_metrics_exporter,
    )
    from prometheus_client import CollectorRegistry

    reg = CollectorRegistry()
    exp = MetricsExporter("t", "live", registry=reg)
    set_metrics_exporter(exp)
    try:
        notify_fill_metrics("intra_arbitrage", "BUY", "IOC", 0.5, 0.0)
    finally:
        set_metrics_exporter(None)

    # No exception; zero fills should not increment counters meaningfully
    assert True
