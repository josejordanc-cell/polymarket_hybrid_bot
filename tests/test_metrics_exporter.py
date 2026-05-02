"""Smoke test for Prometheus /metrics HTTP exposition."""

from __future__ import annotations

import socket
import urllib.request


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_metrics_exporter_http_smoke() -> None:
    from prometheus_client import CollectorRegistry

    from observability.metrics_exporter import MetricsExporter

    port = _free_port()
    registry = CollectorRegistry()
    exp = MetricsExporter("smoke-test", "sim", registry=registry)
    exp.start(port)

    exp.update_equity(123.45)
    exp.update_drift(-0.25)
    exp.update_active_markets(42)
    exp.record_fill("intra_arbitrage", "BUY", "IOC", 99.5)

    url = f"http://127.0.0.1:{port}/metrics"
    with urllib.request.urlopen(url, timeout=5) as resp:
        body = resp.read().decode()

    assert "bot_equity_usd" in body
    assert "bot_equity_drift_pct" in body
    assert "experiment_id" in body
    assert "smoke-test" in body
    assert "mode" in body
    assert "sim" in body
    assert "123.45" in body
    assert "bot_active_markets" in body
    assert "42" in body
    assert "bot_fills_total" in body
    assert "intra_arbitrage" in body
    assert "bot_fill_notional_usd_total" in body
