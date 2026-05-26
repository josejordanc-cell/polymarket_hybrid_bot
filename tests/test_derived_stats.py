"""tests/test_derived_stats.py

Unit tests for utils/derived_stats.py (profitability epic PR1).
Targets each compute helper directly with constructed inputs (EquityPoint
instances for the equity-history helpers; tmp_path JSONL files for
count_total_trades). No full-snapshot integration test — out of scope
per PR1 spec; coverage is on the helpers themselves.
"""
from __future__ import annotations

import json
from collections import deque
from datetime import timedelta
from pathlib import Path

import pytest

from api.schemas import EquityPoint
from utils.derived_stats import (
    _iter_trade_records,
    compute_pnl_all_time,
    compute_pnl_month,
    count_total_trades,
)
from utils.types import utc_now


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ep(day: str, equity: float, pnl: float = 0.0) -> EquityPoint:
    return EquityPoint(day=day, equity=equity, pnl=pnl)


def _current_month_prefix() -> str:
    return utc_now().strftime("%Y-%m")


def _prior_month_prefix() -> str:
    # Move 35 days back to guarantee crossing a month boundary regardless of
    # which day of the month it currently is.
    return (utc_now() - timedelta(days=35)).strftime("%Y-%m")


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, separators=(",", ":")) + "\n")


# ---------------------------------------------------------------------------
# compute_pnl_month
# ---------------------------------------------------------------------------


def test_pnl_month_current_month_with_two_points() -> None:
    """≥2 points in current UTC month → returns last - first equity."""
    mo = _current_month_prefix()
    history = [
        _ep(f"{mo}-01", 1000.0),
        _ep(f"{mo}-15", 1250.0),
    ]
    assert compute_pnl_month(history) == 250.0


def test_pnl_month_empty_history_returns_zero() -> None:
    assert compute_pnl_month([]) == 0.0


def test_pnl_month_single_point_in_current_month_returns_zero() -> None:
    """compute_monthly_pnl lock 6 drops months with <2 points; helper
    should return 0.0 (the absent-row branch)."""
    mo = _current_month_prefix()
    history = [_ep(f"{mo}-10", 1000.0)]
    assert compute_pnl_month(history) == 0.0


def test_pnl_month_no_current_month_row_returns_zero() -> None:
    """History has points only in a prior month → no current-month row →
    returns 0.0."""
    prior = _prior_month_prefix()
    history = [
        _ep(f"{prior}-01", 1000.0),
        _ep(f"{prior}-20", 1100.0),
    ]
    assert compute_pnl_month(history) == 0.0


# ---------------------------------------------------------------------------
# compute_pnl_all_time
# ---------------------------------------------------------------------------


def test_pnl_all_time_multi_point_positive() -> None:
    history = [
        _ep("2026-04-01", 1000.0),
        _ep("2026-04-15", 1100.0),
        _ep("2026-05-01", 1250.0),
    ]
    assert compute_pnl_all_time(history) == 250.0


def test_pnl_all_time_zero_or_one_point_returns_zero() -> None:
    assert compute_pnl_all_time([]) == 0.0
    assert compute_pnl_all_time([_ep("2026-05-01", 1000.0)]) == 0.0


def test_pnl_all_time_negative_drawdown() -> None:
    """current < oldest → negative result (drawdown)."""
    history = [
        _ep("2026-04-01", 1000.0),
        _ep("2026-05-01", 850.0),
    ]
    assert compute_pnl_all_time(history) == pytest.approx(-150.0)


def test_pnl_all_time_works_with_deque() -> None:
    """BotState.equity_history is a deque; helper must support it (uses
    [0] and [-1] indexing which both list and deque expose)."""
    history = deque(
        [_ep("2026-04-01", 1000.0), _ep("2026-05-01", 1500.0)],
        maxlen=200,
    )
    assert compute_pnl_all_time(history) == 500.0


# ---------------------------------------------------------------------------
# count_total_trades
# ---------------------------------------------------------------------------


def test_count_total_trades_counts_only_filled(tmp_path: Path) -> None:
    """Only rows with status=="filled" are counted. The "filled" literal
    matches utils/trades.py:92 verbatim."""
    out = tmp_path / "trades.jsonl"
    _write_jsonl(
        out,
        [
            {"status": "filled", "market_id": "0xa"},
            {"status": "filled", "market_id": "0xb"},
            {"status": "filled", "market_id": "0xc"},
        ],
    )
    assert count_total_trades(path=out) == 3


def test_count_total_trades_excludes_non_filled_statuses(tmp_path: Path) -> None:
    """Cancelled / canceled / rejected / pending / missing-status / anything
    not literally 'filled' is excluded."""
    out = tmp_path / "trades.jsonl"
    _write_jsonl(
        out,
        [
            {"status": "filled", "market_id": "0xa"},
            {"status": "cancelled", "market_id": "0xb"},
            {"status": "canceled", "market_id": "0xc"},
            {"status": "rejected", "market_id": "0xd"},
            {"status": "pending", "market_id": "0xe"},
            {"status": "FILLED", "market_id": "0xf"},  # case-sensitive: excluded
            {"market_id": "0xg"},  # no status field at all
            {"status": "filled", "market_id": "0xh"},
        ],
    )
    assert count_total_trades(path=out) == 2  # only the two "filled" rows


def test_count_total_trades_missing_file_returns_zero(tmp_path: Path) -> None:
    nonexistent = tmp_path / "no_such_file.jsonl"
    assert not nonexistent.exists()
    assert count_total_trades(path=nonexistent) == 0


def test_count_total_trades_empty_file_returns_zero(tmp_path: Path) -> None:
    out = tmp_path / "trades.jsonl"
    out.touch()
    assert out.is_file() and out.stat().st_size == 0
    assert count_total_trades(path=out) == 0


def test_count_total_trades_tolerates_blank_and_malformed_lines(tmp_path: Path) -> None:
    out = tmp_path / "trades.jsonl"
    # Mix of well-formed JSON, blank lines, whitespace lines, and garbage.
    out.write_text(
        '{"status": "filled", "market_id": "0xa"}\n'
        "\n"
        "   \n"
        "this is not json\n"
        '{"status": "filled", "market_id": "0xb"}\n'
        "{broken json...\n"
        '{"status": "filled", "market_id": "0xc"}\n',
        encoding="utf-8",
    )
    assert count_total_trades(path=out) == 3


def test_count_total_trades_uses_bot_data_dir_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When path is None, the default resolves via
    Path(os.environ.get('BOT_DATA_DIR', 'data')) / 'trades.jsonl' —
    matching utils/trades.py:45 verbatim."""
    monkeypatch.setenv("BOT_DATA_DIR", str(tmp_path))
    out = tmp_path / "trades.jsonl"
    _write_jsonl(
        out,
        [
            {"status": "filled", "market_id": "0xa"},
            {"status": "cancelled", "market_id": "0xb"},
        ],
    )
    assert count_total_trades() == 1  # default path resolution works


# ---------------------------------------------------------------------------
# _iter_trade_records (private reader)
# ---------------------------------------------------------------------------


def test_iter_trade_records_missing_file_yields_nothing(tmp_path: Path) -> None:
    nonexistent = tmp_path / "missing.jsonl"
    assert list(_iter_trade_records(nonexistent)) == []


def test_iter_trade_records_skips_blank_lines(tmp_path: Path) -> None:
    out = tmp_path / "trades.jsonl"
    out.write_text(
        '{"a": 1}\n\n   \n{"b": 2}\n',
        encoding="utf-8",
    )
    records = list(_iter_trade_records(out))
    assert records == [{"a": 1}, {"b": 2}]
