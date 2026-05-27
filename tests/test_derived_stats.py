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
    _iter_jsonl_records,
    _parse_trade_ts,
    build_daily_equity_series_from_trades,
    compute_pnl_all_time,
    compute_pnl_month,
    compute_streak,
    compute_win_rate,
    count_total_trades,
)
from utils.risk_metrics import compute_sharpe, compute_sortino
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
# _iter_jsonl_records (private reader)
# ---------------------------------------------------------------------------


def test_iter_jsonl_records_missing_file_yields_nothing(tmp_path: Path) -> None:
    nonexistent = tmp_path / "missing.jsonl"
    assert list(_iter_jsonl_records(nonexistent)) == []


def test_iter_jsonl_records_skips_blank_lines(tmp_path: Path) -> None:
    out = tmp_path / "trades.jsonl"
    out.write_text(
        '{"a": 1}\n\n   \n{"b": 2}\n',
        encoding="utf-8",
    )
    records = list(_iter_jsonl_records(out))
    assert records == [{"a": 1}, {"b": 2}]


# ---------------------------------------------------------------------------
# compute_win_rate (PR2)
# ---------------------------------------------------------------------------


def _write_settlements(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, separators=(",", ":")) + "\n")


def test_win_rate_all_wins(tmp_path: Path) -> None:
    # NOTE: compute_win_rate returns a PERCENTAGE on a 0–100 scale
    # (locked cross-repo contract — dashboard appends "%" and does NOT
    # multiply by 100). All-wins → 100.0, not 1.0.
    out = tmp_path / "settlements.jsonl"
    _write_settlements(
        out,
        [
            {"pnl": 1.0, "market_id": "0xa"},
            {"pnl": 2.5, "market_id": "0xb"},
            {"pnl": 0.5, "market_id": "0xc"},
        ],
    )
    assert compute_win_rate(path=out) == 100.0


def test_win_rate_all_losses(tmp_path: Path) -> None:
    out = tmp_path / "settlements.jsonl"
    _write_settlements(
        out,
        [
            {"pnl": -1.0, "market_id": "0xa"},
            {"pnl": -0.1, "market_id": "0xb"},
        ],
    )
    assert compute_win_rate(path=out) == 0.0


def test_win_rate_mixed(tmp_path: Path) -> None:
    """2 wins out of 4 non-break-even rows → 50.0 (0–100 scale)."""
    out = tmp_path / "settlements.jsonl"
    _write_settlements(
        out,
        [
            {"pnl": 1.0, "market_id": "0xa"},   # win
            {"pnl": -1.0, "market_id": "0xb"},  # loss
            {"pnl": 2.0, "market_id": "0xc"},   # win
            {"pnl": -3.0, "market_id": "0xd"},  # loss
        ],
    )
    assert compute_win_rate(path=out) == 50.0


def test_win_rate_excludes_break_evens_from_denominator(tmp_path: Path) -> None:
    """pnl == 0 rows are excluded from BOTH numerator and denominator.
    1 win + 1 loss + 2 break-evens → 1/2 = 50.0 (0–100 scale; not 25.0
    from 1/4)."""
    out = tmp_path / "settlements.jsonl"
    _write_settlements(
        out,
        [
            {"pnl": 1.0, "market_id": "0xa"},
            {"pnl": 0.0, "market_id": "0xb"},  # excluded
            {"pnl": -1.0, "market_id": "0xc"},
            {"pnl": 0.0, "market_id": "0xd"},  # excluded
        ],
    )
    assert compute_win_rate(path=out) == 50.0


def test_win_rate_missing_file_returns_zero(tmp_path: Path) -> None:
    assert compute_win_rate(path=tmp_path / "missing.jsonl") == 0.0


def test_win_rate_empty_file_returns_zero(tmp_path: Path) -> None:
    out = tmp_path / "settlements.jsonl"
    out.touch()
    assert compute_win_rate(path=out) == 0.0


def test_win_rate_all_break_evens_returns_zero(tmp_path: Path) -> None:
    """All-break-even → denominator 0 → return 0.0 (not divide-by-zero)."""
    out = tmp_path / "settlements.jsonl"
    _write_settlements(
        out,
        [
            {"pnl": 0.0, "market_id": "0xa"},
            {"pnl": 0.0, "market_id": "0xb"},
        ],
    )
    assert compute_win_rate(path=out) == 0.0


def test_win_rate_uses_bot_data_dir_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BOT_DATA_DIR", str(tmp_path))
    out = tmp_path / "settlements.jsonl"
    _write_settlements(
        out,
        [
            {"pnl": 1.0, "market_id": "0xa"},
            {"pnl": -1.0, "market_id": "0xb"},
        ],
    )
    assert compute_win_rate() == 50.0  # default-path resolution works (0–100 scale)


# ---------------------------------------------------------------------------
# compute_streak (PR2)
# ---------------------------------------------------------------------------


def test_streak_three_wins_in_a_row(tmp_path: Path) -> None:
    out = tmp_path / "settlements.jsonl"
    _write_settlements(
        out,
        [
            {"pnl": 1.0, "market_id": "0xa"},
            {"pnl": 1.0, "market_id": "0xb"},
            {"pnl": 1.0, "market_id": "0xc"},
        ],
    )
    assert compute_streak(path=out) == 3


def test_streak_two_losses_in_a_row_negative(tmp_path: Path) -> None:
    """Loss streak returns NEGATIVE int."""
    out = tmp_path / "settlements.jsonl"
    _write_settlements(
        out,
        [
            {"pnl": -1.0, "market_id": "0xa"},
            {"pnl": -1.0, "market_id": "0xb"},
        ],
    )
    assert compute_streak(path=out) == -2


def test_streak_most_recent_break_even_returns_zero(tmp_path: Path) -> None:
    """Break-even at the most recent row → streak resets to 0."""
    out = tmp_path / "settlements.jsonl"
    _write_settlements(
        out,
        [
            {"pnl": 1.0, "market_id": "0xa"},
            {"pnl": 1.0, "market_id": "0xb"},
            {"pnl": 0.0, "market_id": "0xc"},  # most recent → break-even
        ],
    )
    assert compute_streak(path=out) == 0


def test_streak_break_even_mid_history_doesnt_affect_current(tmp_path: Path) -> None:
    """A break-even mid-history doesn't extend across it. Current streak
    counts only from most-recent backward until the first sign-break."""
    out = tmp_path / "settlements.jsonl"
    _write_settlements(
        out,
        [
            {"pnl": 1.0, "market_id": "0xa"},   # win (old)
            {"pnl": 0.0, "market_id": "0xb"},   # break-even
            {"pnl": 1.0, "market_id": "0xc"},   # win
            {"pnl": 1.0, "market_id": "0xd"},   # win (most recent)
        ],
    )
    # From the end backward: win, win, then break-even stops the count → streak = 2
    assert compute_streak(path=out) == 2


def test_streak_ordering_respected_streak_broken_by_sign_change(tmp_path: Path) -> None:
    """Streak counts from most recent backward until sign flips. A loss
    after wins (or vice-versa) breaks the streak at the boundary."""
    out = tmp_path / "settlements.jsonl"
    _write_settlements(
        out,
        [
            {"pnl": -1.0, "market_id": "0xa"},
            {"pnl": -1.0, "market_id": "0xb"},
            {"pnl": 1.0, "market_id": "0xc"},   # win
            {"pnl": 1.0, "market_id": "0xd"},   # win (most recent)
        ],
    )
    # Most-recent-backward: win, win, then a loss → streak = 2 (wins)
    assert compute_streak(path=out) == 2


def test_streak_missing_file_returns_zero(tmp_path: Path) -> None:
    assert compute_streak(path=tmp_path / "missing.jsonl") == 0


def test_streak_empty_file_returns_zero(tmp_path: Path) -> None:
    out = tmp_path / "settlements.jsonl"
    out.touch()
    assert compute_streak(path=out) == 0


def test_streak_single_win_returns_one(tmp_path: Path) -> None:
    out = tmp_path / "settlements.jsonl"
    _write_settlements(out, [{"pnl": 1.0, "market_id": "0xa"}])
    assert compute_streak(path=out) == 1


def test_streak_single_loss_returns_negative_one(tmp_path: Path) -> None:
    out = tmp_path / "settlements.jsonl"
    _write_settlements(out, [{"pnl": -1.0, "market_id": "0xa"}])
    assert compute_streak(path=out) == -1


def test_streak_uses_bot_data_dir_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BOT_DATA_DIR", str(tmp_path))
    out = tmp_path / "settlements.jsonl"
    _write_settlements(
        out,
        [
            {"pnl": 1.0, "market_id": "0xa"},
            {"pnl": 1.0, "market_id": "0xb"},
        ],
    )
    assert compute_streak() == 2


# ---------------------------------------------------------------------------
# build_daily_equity_series_from_trades — offline weekly-report consumer.
# Covers per-day bucketing (last equity_after wins), forward-fill across
# gap days, defensive skips (cancelled / missing equity_after / malformed
# ts), and end-to-end gating into compute_sharpe / compute_sortino.
# ---------------------------------------------------------------------------


def _trade_row(
    ts: str,
    equity_after: float | None,
    status: str = "filled",
) -> dict:
    row: dict = {"ts": ts, "status": status, "market_id": "0xm"}
    if equity_after is not None:
        row["equity_after"] = equity_after
    return row


def test_build_series_missing_file_returns_empty(tmp_path: Path) -> None:
    assert build_daily_equity_series_from_trades(path=tmp_path / "absent.jsonl") == []


def test_build_series_empty_file_returns_empty(tmp_path: Path) -> None:
    out = tmp_path / "trades.jsonl"
    out.write_text("", encoding="utf-8")
    assert build_daily_equity_series_from_trades(path=out) == []


def test_build_series_last_equity_per_day(tmp_path: Path) -> None:
    """Multiple fills on the same UTC day → last equity_after wins."""
    out = tmp_path / "trades.jsonl"
    _write_jsonl(
        out,
        [
            _trade_row("2026-05-01T09:00:00Z", 10000.0),
            _trade_row("2026-05-01T12:00:00Z", 10050.0),
            _trade_row("2026-05-01T18:00:00Z", 10075.0),  # last on day 1
            _trade_row("2026-05-02T10:00:00Z", 10100.0),
        ],
    )
    series = build_daily_equity_series_from_trades(path=out)
    assert len(series) == 2
    assert series[0].day == "2026-05-01"
    assert series[0].equity == 10075.0  # last write wins
    assert series[1].day == "2026-05-02"
    assert series[1].equity == 10100.0


def test_build_series_forward_fill_across_gap_yields_zero_return(tmp_path: Path) -> None:
    """Day 1 fill, day 3 fill, no day 2 fill → series has 3 points
    (day 1, day 2 = day 1's equity by forward-fill, day 3). The
    implied return on day 2 is therefore 0.0."""
    out = tmp_path / "trades.jsonl"
    _write_jsonl(
        out,
        [
            _trade_row("2026-05-01T10:00:00Z", 10000.0),
            _trade_row("2026-05-03T10:00:00Z", 10200.0),
        ],
    )
    series = build_daily_equity_series_from_trades(path=out)
    assert [pt.day for pt in series] == ["2026-05-01", "2026-05-02", "2026-05-03"]
    assert [pt.equity for pt in series] == [10000.0, 10000.0, 10200.0]
    # Implied returns: day 2 = (10000-10000)/10000 = 0.0, day 3 = 0.02
    equities = [pt.equity for pt in series]
    day2_return = (equities[1] - equities[0]) / equities[0]
    assert day2_return == 0.0


def test_build_series_skips_non_filled_status(tmp_path: Path) -> None:
    """Cancelled / rejected / missing-status rows must not contribute."""
    out = tmp_path / "trades.jsonl"
    _write_jsonl(
        out,
        [
            _trade_row("2026-05-01T10:00:00Z", 10000.0, status="filled"),
            _trade_row("2026-05-02T10:00:00Z", 99999.0, status="cancelled"),
            _trade_row("2026-05-03T10:00:00Z", 88888.0, status="rejected"),
        ],
    )
    series = build_daily_equity_series_from_trades(path=out)
    assert len(series) == 1
    assert series[0].equity == 10000.0


def test_build_series_skips_rows_without_equity_after(tmp_path: Path) -> None:
    """Defensive: rows missing equity_after (e.g. legacy or partial
    writes) are skipped, not crashed on."""
    out = tmp_path / "trades.jsonl"
    _write_jsonl(
        out,
        [
            _trade_row("2026-05-01T10:00:00Z", None),  # missing
            _trade_row("2026-05-02T10:00:00Z", 10100.0),
        ],
    )
    series = build_daily_equity_series_from_trades(path=out)
    assert len(series) == 1
    assert series[0].day == "2026-05-02"
    assert series[0].equity == 10100.0


def test_build_series_skips_malformed_ts(tmp_path: Path) -> None:
    out = tmp_path / "trades.jsonl"
    _write_jsonl(
        out,
        [
            _trade_row("not-a-date", 99999.0),
            _trade_row("2026-05-01T10:00:00Z", 10000.0),
        ],
    )
    series = build_daily_equity_series_from_trades(path=out)
    assert len(series) == 1
    assert series[0].equity == 10000.0


def test_build_series_uses_bot_data_dir_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No explicit path → resolves BOT_DATA_DIR/trades.jsonl fresh."""
    monkeypatch.setenv("BOT_DATA_DIR", str(tmp_path))
    out = tmp_path / "trades.jsonl"
    _write_jsonl(out, [_trade_row("2026-05-01T10:00:00Z", 10000.0)])
    series = build_daily_equity_series_from_trades()
    assert len(series) == 1


def test_build_series_accepts_offset_iso_ts(tmp_path: Path) -> None:
    """``+00:00`` offset form (as in tests/fixtures/sample_trades.jsonl)
    parses identically to the ``Z`` suffix form."""
    out = tmp_path / "trades.jsonl"
    _write_jsonl(
        out,
        [
            _trade_row("2026-05-01T10:00:00+00:00", 10000.0),
            _trade_row("2026-05-02T10:00:00+00:00", 10100.0),
        ],
    )
    series = build_daily_equity_series_from_trades(path=out)
    assert [pt.day for pt in series] == ["2026-05-01", "2026-05-02"]


def test_build_series_under_20_days_gates_sharpe_sortino_to_none(
    tmp_path: Path,
) -> None:
    """End-to-end: <20 daily returns → compute_sharpe / compute_sortino
    BOTH return None (the locked ≥20 honesty gate fires on real
    weekly-report-shaped input)."""
    out = tmp_path / "trades.jsonl"
    # 10 sequential days of fills → 10 points → 9 returns → both gate.
    rows = []
    eq = 10000.0
    for i in range(1, 11):
        eq *= 1.005
        rows.append(_trade_row(f"2026-05-{i:02d}T10:00:00Z", eq))
    _write_jsonl(out, rows)
    series = build_daily_equity_series_from_trades(path=out)
    assert len(series) == 10
    assert compute_sharpe(series) is None
    assert compute_sortino(series) is None


def test_build_series_20plus_days_lets_sharpe_compute(tmp_path: Path) -> None:
    """Positive control to the gating test: with ≥21 points (=20 daily
    returns), Sharpe returns a finite number on non-zero-vol input."""
    out = tmp_path / "trades.jsonl"
    rows = []
    eq = 10000.0
    # Alternate gain/loss to guarantee non-zero std.
    multipliers = [1.01 if i % 2 == 0 else 0.995 for i in range(21)]
    for i, m in enumerate(multipliers, start=1):
        eq *= m
        rows.append(_trade_row(f"2026-05-{i:02d}T10:00:00Z", eq))
    _write_jsonl(out, rows)
    series = build_daily_equity_series_from_trades(path=out)
    assert len(series) == 21
    sharpe = compute_sharpe(series)
    assert sharpe is not None


def test_parse_trade_ts_handles_both_formats() -> None:
    assert _parse_trade_ts("2026-05-01T10:00:00.123Z") is not None
    assert _parse_trade_ts("2026-05-01T10:00:00+00:00") is not None
    assert _parse_trade_ts("not-a-date") is None
    assert _parse_trade_ts(None) is None
    assert _parse_trade_ts(12345) is None
