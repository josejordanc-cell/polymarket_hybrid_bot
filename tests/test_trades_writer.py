"""tests/test_trades_writer.py

Unit tests for utils/trades.py (profitability epic piece (1)).
Targets persist_fill() directly with tmp_path; no integration with
_exec_and_record (see CHANGELOG [Unreleased] owed-gap note).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from utils.trades import _resolve_trades_path, persist_fill


def _read_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_single_row_written(tmp_path: Path) -> None:
    out = tmp_path / "trades.jsonl"
    persist_fill(
        market_id="0xabc",
        outcome="YES",
        side="BUY",
        strategy="intra_arbitrage",
        fill_price=0.5234,
        filled_size=100.0,
        fee_usdc=0.10468,
        equity_after=9876.54,
        tif="IOC",
        order_id="order-1",
        path=out,
    )
    rows = _read_lines(out)
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "filled"
    assert row["market_id"] == "0xabc"
    assert row["outcome"] == "YES"
    assert row["side"] == "BUY"
    assert row["strategy"] == "intra_arbitrage"
    assert row["price"] == 0.5234
    assert row["size"] == 100.0
    assert row["fee_usdc"] == pytest.approx(0.10468)
    assert row["equity_after"] == 9876.54
    assert row["tif"] == "IOC"
    assert row["order_id"] == "order-1"
    assert row["ts"].endswith("Z")
    assert "T" in row["ts"]  # ISO 8601 separator


def test_optional_fields_default_none(tmp_path: Path) -> None:
    out = tmp_path / "trades.jsonl"
    persist_fill(
        market_id="0xabc", outcome="NO", side="SELL", strategy="market_maker",
        fill_price=0.5, filled_size=10.0, fee_usdc=0.01, equity_after=100.0,
        path=out,
    )
    row = _read_lines(out)[0]
    assert row["tif"] is None
    assert row["order_id"] is None


def test_sequential_appends(tmp_path: Path) -> None:
    out = tmp_path / "trades.jsonl"
    for i in range(5):
        persist_fill(
            market_id=f"0x{i}", outcome="YES", side="BUY", strategy="momentum_latency",
            fill_price=0.5, filled_size=float(i + 1), fee_usdc=0.0, equity_after=100.0,
            path=out,
        )
    rows = _read_lines(out)
    assert len(rows) == 5
    assert [r["market_id"] for r in rows] == [f"0x{i}" for i in range(5)]


def test_missing_parent_dir_created(tmp_path: Path) -> None:
    out = tmp_path / "nested" / "deep" / "trades.jsonl"
    assert not out.parent.exists()
    persist_fill(
        market_id="0xabc", outcome="YES", side="BUY", strategy="intra_arbitrage",
        fill_price=0.5, filled_size=10.0, fee_usdc=0.01, equity_after=100.0,
        path=out,
    )
    assert out.exists()
    assert len(_read_lines(out)) == 1


def test_permission_error_swallowed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed open() must log and return None, not raise."""
    out = tmp_path / "trades.jsonl"
    def _boom(*_args, **_kwargs):
        raise PermissionError("denied")
    monkeypatch.setattr(Path, "open", _boom)
    # Must not raise
    persist_fill(
        market_id="0xabc", outcome="YES", side="BUY", strategy="intra_arbitrage",
        fill_price=0.5, filled_size=10.0, fee_usdc=0.01, equity_after=100.0,
        path=out,
    )
    assert not out.exists()


def test_bot_data_dir_env_respected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_DATA_DIR", str(tmp_path))
    path = _resolve_trades_path()
    assert path == tmp_path / "trades.jsonl"


def test_bot_data_dir_default_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BOT_DATA_DIR", raising=False)
    path = _resolve_trades_path()
    assert path == Path("data") / "trades.jsonl"


def test_roundtrip_with_weekly_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """persist_fill output is consumable by scripts/weekly_report.generate_report."""
    from scripts.weekly_report import generate_report

    monkeypatch.setenv("BOT_DATA_DIR", str(tmp_path))
    out = tmp_path / "trades.jsonl"
    for i in range(3):
        persist_fill(
            market_id=f"0x{i}", outcome="YES", side="BUY", strategy="intra_arbitrage",
            fill_price=0.5, filled_size=10.0, fee_usdc=0.02, equity_after=100.0 + i,
            path=out,
        )
    report = generate_report(weeks=1)
    assert report.trades_total == 3
    assert report.trades_filled == 3
    assert report.fees_paid_usdc == pytest.approx(0.06)


def test_concurrent_writes_no_corruption(tmp_path: Path) -> None:
    """50 async-gathered persist_fill calls produce 50 well-formed JSON lines."""
    import asyncio

    out = tmp_path / "trades.jsonl"

    async def _drive() -> None:
        async def _one(i: int) -> None:
            persist_fill(
                market_id=f"0x{i}", outcome="YES", side="BUY",
                strategy="intra_arbitrage",
                fill_price=0.5, filled_size=float(i + 1), fee_usdc=0.0,
                equity_after=100.0, path=out,
            )
        await asyncio.gather(*(_one(i) for i in range(50)))

    asyncio.run(_drive())
    rows = _read_lines(out)
    assert len(rows) == 50
    sizes = sorted(r["size"] for r in rows)
    assert sizes == [float(i + 1) for i in range(50)]


def test_zero_fee_accepted(tmp_path: Path) -> None:
    """Some sim-mode fills have fee_usdc == 0.0 — must round-trip cleanly."""
    out = tmp_path / "trades.jsonl"
    persist_fill(
        market_id="0xabc", outcome="YES", side="BUY", strategy="intra_arbitrage",
        fill_price=0.5, filled_size=10.0, fee_usdc=0.0, equity_after=100.0,
        path=out,
    )
    row = _read_lines(out)[0]
    assert row["fee_usdc"] == 0.0
