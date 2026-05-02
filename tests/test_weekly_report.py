"""Weekly JSON log report (Prompt 7)."""
from __future__ import annotations

import json
import subprocess
import sys
from io import StringIO
from pathlib import Path

import pytest

from scripts.weekly_report import (
    _parse_end_instant_utc,
    _parse_start_instant_utc,
    build_report,
    main,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "sample_logs.jsonl"


def _lines():
    with FIXTURE.open(encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            yield FIXTURE, lineno, json.loads(line)


def test_build_report_sample_fixture_totals() -> None:
    start = _parse_start_instant_utc("2026-04-26")
    end = _parse_end_instant_utc("2026-05-02")
    rep = build_report(_lines(), start, end, "default")

    assert rep["experiment_id"] == "default"
    assert rep["mode"] == "sim"
    assert rep["trade_count"] == 18
    assert rep["strategies"]["intra_arbitrage"]["bundles_completed"] == 3
    assert rep["incidents"]["polymarket_ws_closed"] == 1
    assert rep["incidents"]["live_order_rejected"] == 1
    assert rep["incidents"]["kill_switch_triggered"] == 1
    assert rep["market_resolved_count"] == 1
    assert len(rep["arb_unwind_skipped_notes"]) == 1

    # Bundle PnL includes unwind compensation on b-unwind-1
    arb_total = rep["strategies"]["intra_arbitrage"]["total_pnl_usd"]
    # Legs + unwind for first bundle; two more bundles without unwind fill
    b1 = -(45 + 0.09) - (53 + 0.106) + (4.4 - 0.0088)  # SELL unwind
    b2 = -(20 + 0.04) - (29 + 0.058)
    b3 = -(33.6 + 0.0672) - (33 + 0.066)
    assert pytest.approx(arb_total, rel=1e-5) == b1 + b2 + b3

    assert rep["starting_equity_usd"] == 9990.91
    assert rep["ending_equity_usd"] == 9684.14


def test_main_markdown_stdout() -> None:
    buf = StringIO()
    code = main(
        [
            "--logs",
            str(FIXTURE),
            "--start",
            "2026-04-26",
            "--end",
            "2026-05-02",
            "--experiment-id",
            "default",
            "--format",
            "markdown",
        ],
        stdout=buf,
    )
    assert code == 0
    out = buf.getvalue()
    assert "| Week |" in out
    assert "intra_arbitrage" in out
    assert "Arb unwind skipped" in out


def test_main_json_parseable() -> None:
    buf = StringIO()
    code = main(
        [
            "--logs",
            str(FIXTURE),
            "--start",
            "2026-04-26",
            "--end",
            "2026-05-02",
            "--experiment-id",
            "default",
            "--format",
            "json",
        ],
        stdout=buf,
    )
    assert code == 0
    data = json.loads(buf.getvalue())
    assert data["best_performer"] in data["strategies"]


def test_experiment_id_required_lists_ids() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "scripts" / "weekly_report.py"),
            "--logs",
            str(FIXTURE),
            "--start",
            "2026-04-26",
            "--end",
            "2026-05-02",
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 2
    assert "arm_a" in proc.stderr
    assert "default" in proc.stderr
    assert proc.stdout == ""


def test_cli_invocation_matches_acceptance() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "scripts" / "weekly_report.py"),
            "--logs",
            str(FIXTURE),
            "--start",
            "2026-04-26",
            "--end",
            "2026-05-02",
            "--experiment-id",
            "default",
            "--format",
            "markdown",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert "| Metric | Value |" in proc.stdout
