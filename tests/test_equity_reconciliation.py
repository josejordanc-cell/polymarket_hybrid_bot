"""Tests for on-chain USDC vs bot equity reconciliation (Prompt 9)."""
from __future__ import annotations

import json

import pytest

from config.settings import get_config
from utils.equity_reconciliation import (
    DriftEvaluation,
    compute_drift_pct,
    maybe_alert_equity_drift,
    parse_bot_equity_from_metrics_text,
)
from utils.wallet_reconcile_cli import main as wallet_reconcile_main


def test_compute_drift_10500_vs_10000() -> None:
    du, dp = compute_drift_pct(10_500.0, 10_000.0)
    assert du == 500.0
    assert dp == 5.0
    ev = DriftEvaluation.from_balances(10_500.0, 10_000.0, 1.0)
    assert ev.drift_usd == 500.0
    assert ev.drift_pct == 5.0
    assert ev.breaches_threshold is True


@pytest.mark.asyncio
async def test_drift_exceeding_threshold_fires_alert() -> None:
    ev = DriftEvaluation.from_balances(10_500.0, 10_000.0, 1.0)
    assert ev.breaches_threshold
    alerts: list[tuple[str, str]] = []

    async def capture(msg: str, level: str = "info", **kwargs: object) -> None:
        alerts.append((msg, level))

    fired = await maybe_alert_equity_drift(
        drift_pct=ev.drift_pct,
        drift_usd=ev.drift_usd,
        on_chain_usdc=10_500.0,
        bot_equity_usd=10_000.0,
        threshold_pct=1.0,
        send_alert=capture,
    )
    assert fired is True
    assert len(alerts) == 1
    assert alerts[0][1] == "warning"
    assert "5.00%" in alerts[0][0] or "5.0" in alerts[0][0]


def test_parse_bot_equity_from_metrics_text() -> None:
    text = (
        "# HELP bot_equity_usd x\n"
        "# TYPE bot_equity_usd gauge\n"
        'bot_equity_usd{experiment_id="default",mode="sim"} 10000\n'
    )
    v = parse_bot_equity_from_metrics_text(text, "default", "sim")
    assert v == 10_000.0


def test_cli_threshold_10_allows_5pct_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_POLYGON_RPC_URL", "https://polygon.invalid/rpc")
    get_config.cache_clear()

    monkeypatch.setattr(
        "utils.wallet_reconcile_cli.get_polygon_wallet_address",
        lambda _c: "0x1111111111111111111111111111111111111111",
    )
    monkeypatch.setattr(
        "utils.wallet_reconcile_cli._build_web3",
        lambda _url: object(),
    )
    monkeypatch.setattr(
        "utils.wallet_reconcile_cli.fetch_polygon_usdc_human",
        lambda _w3, _w: (10_500.0, "native_usdc"),
    )
    monkeypatch.setattr(
        "utils.wallet_reconcile_cli.fetch_bot_equity_from_metrics",
        lambda *args, **kwargs: 10_000.0,
    )

    try:
        code = wallet_reconcile_main(["--threshold", "10"])
    finally:
        get_config.cache_clear()

    assert code == 0


def test_cli_log_fallback_reads_equity(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    log = tmp_path / "bot.log"
    log.write_text(
        json.dumps(
            {
                "event": "fill_recorded",
                "equity": 10_000.0,
                "market": "m",
            }
        )
        + "\n"
    )
    monkeypatch.setenv("BOT_POLYGON_RPC_URL", "https://polygon.invalid/rpc")
    get_config.cache_clear()

    monkeypatch.setattr(
        "utils.wallet_reconcile_cli.get_polygon_wallet_address",
        lambda _c: "0x1111111111111111111111111111111111111111",
    )
    monkeypatch.setattr(
        "utils.wallet_reconcile_cli._build_web3",
        lambda _url: object(),
    )
    monkeypatch.setattr(
        "utils.wallet_reconcile_cli.fetch_polygon_usdc_human",
        lambda _w3, _w: (10_500.0, "native_usdc"),
    )
    monkeypatch.setattr(
        "utils.wallet_reconcile_cli.fetch_bot_equity_from_metrics",
        lambda *args, **kwargs: None,
    )

    try:
        code = wallet_reconcile_main(["--no-metrics", "--log-file", str(log), "--threshold", "10"])
    finally:
        get_config.cache_clear()

    assert code == 0
