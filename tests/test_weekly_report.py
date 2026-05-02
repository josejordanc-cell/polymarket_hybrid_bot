"""Tests for v0.2.1 ``scripts/weekly_report`` (trades.jsonl aggregates)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts.weekly_report import WeeklyReport, generate_report, main, write_report

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "sample_trades.jsonl"
ANCHOR = datetime(2026, 5, 2, 15, 0, tzinfo=timezone.utc)


def test_weekly_report_dataclass_markdown() -> None:
    r = WeeklyReport(
        week_start="2026-05-01",
        week_end="2026-05-02",
        trades_total=3,
        trades_filled=2,
        trades_cancelled=1,
        realized_pnl_usdc=1.5,
        unrealized_pnl_usdc=0.0,
        fees_paid_usdc=0.02,
    )
    md = r.to_markdown()
    assert "Trades (total)" in md
    assert "| 3 |" in md or "3 |" in md
    d = r.to_dict()
    assert d["trades_total"] == 3


def test_generate_report_sample_fixture_window() -> None:
    rep = generate_report(weeks=4, now=ANCHOR, trade_log=FIXTURE)
    assert rep.trades_total == 2
    assert rep.trades_filled == 1
    assert rep.trades_cancelled == 1
    assert rep.realized_pnl_usdc == pytest.approx(1.25)
    assert rep.fees_paid_usdc == pytest.approx(0.01)


def test_generate_report_missing_file_no_raise() -> None:
    rep = generate_report(weeks=1, now=ANCHOR, trade_log="/no/such/trades.jsonl")
    assert rep.trades_total == 0
    assert any("No trade log" in n for n in rep.notes)


def test_write_report_json(tmp_path: Path) -> None:
    rep = WeeklyReport(
        week_start="2026-05-01",
        week_end="2026-05-02",
        trades_total=1,
        trades_filled=1,
    )
    p = write_report(rep, fmt="json", out_dir=tmp_path)
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["trades_filled"] == 1


def test_main_stdout_with_env_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "trades.jsonl").write_text(
        FIXTURE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    monkeypatch.setenv("BOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BOT_REPORT_DIR", str(tmp_path / "out"))
    code = main(["--weeks", "8", "--format", "json", "--stdout"])
    assert code == 0


def test_cli_module_invocation_zero_exit(tmp_path: Path) -> None:
    (tmp_path / "trades.jsonl").write_text(
        FIXTURE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    repo = Path(__file__).resolve().parents[1]
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.weekly_report",
            "--weeks",
            "8",
            "--format",
            "json",
            "--stdout",
        ],
        cwd=str(repo),
        env={
            **os.environ,
            "BOT_DATA_DIR": str(tmp_path),
            "BOT_REPORT_DIR": str(tmp_path / "rep"),
            "PYTHONPATH": str(repo),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    out = json.loads(proc.stdout)
    assert "trades_total" in out
