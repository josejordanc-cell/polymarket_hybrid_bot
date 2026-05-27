"""
Weekly trade summary from JSONL (v0.2.1).

Reads ``trades.jsonl`` under ``BOT_DATA_DIR`` (default ``./data``). Side-effect free
on import; safe for ``python -m scripts.weekly_report`` from cron.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator


def _utc_now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def _parse_ts(obj: dict[str, Any]) -> datetime | None:
    for key in ("ts", "timestamp", "time", "@timestamp", "filled_at"):
        v = obj.get(key)
        if isinstance(v, (int, float)):
            try:
                return datetime.fromtimestamp(float(v), tz=timezone.utc)
            except (OSError, ValueError):
                continue
        if isinstance(v, str):
            raw = v.strip()
            if raw.endswith("Z"):
                raw = raw[:-1] + "+00:00"
            try:
                dt = datetime.fromisoformat(raw)
            except ValueError:
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
    return None


def _default_data_dir() -> Path:
    return Path(os.environ.get("BOT_DATA_DIR", "data"))


def _default_report_dir() -> Path:
    explicit = os.environ.get("BOT_REPORT_DIR")
    if explicit:
        return Path(explicit)
    return Path(os.environ.get("BOT_DATA_DIR", "data")) / "reports"


def _iter_trade_records(path: Path) -> Iterator[dict[str, Any]]:
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                yield obj


def _status(obj: dict[str, Any]) -> str:
    s = obj.get("status") or obj.get("state") or obj.get("event") or ""
    return str(s).lower()


@dataclass
class WeeklyReport:
    """Aggregated metrics for a trailing calendar window.

    Note on ``sharpe`` / ``sortino``: these are computed over the
    ENTIRE on-disk ``trades.jsonl`` history (not the trailing window),
    because the ≥20-daily-returns honesty gate cannot be satisfied by
    a 7-day window. They are stability metrics, not window aggregates;
    ``None`` indicates insufficient data (fewer than 20 daily returns,
    or — for Sortino — no downside returns at all). See
    ``utils/risk_metrics.compute_sharpe`` / ``compute_sortino``.
    """

    week_start: str
    week_end: str
    trades_total: int = 0
    trades_filled: int = 0
    trades_cancelled: int = 0
    realized_pnl_usdc: float = 0.0
    unrealized_pnl_usdc: float = 0.0
    fees_paid_usdc: float = 0.0
    sharpe: float | None = None
    sortino: float | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d

    @staticmethod
    def _fmt_optional(v: float | None, spec: str = ".4f") -> str:
        """Render an Optional[float] metric for the markdown table.

        ``None`` becomes ``"n/a (insufficient data)"`` — honest across
        all three Sharpe/Sortino None causes (insufficient samples,
        zero volatility, zero downside) without lying with ``"0.00"``.
        """
        return "n/a (insufficient data)" if v is None else format(v, spec)

    def to_markdown(self) -> str:
        lines = [
            "# Weekly trade report",
            "",
            f"| Window | `{self.week_start}` → `{self.week_end}` |",
            "",
            "| Metric | Value |",
            "| --- | ---: |",
            f"| Trades (total) | {self.trades_total} |",
            f"| Filled | {self.trades_filled} |",
            f"| Cancelled | {self.trades_cancelled} |",
            f"| Realized PnL (USDC) | {self.realized_pnl_usdc:.4f} |",
            f"| Unrealized PnL (USDC) | {self.unrealized_pnl_usdc:.4f} |",
            f"| Fees paid (USDC) | {self.fees_paid_usdc:.4f} |",
            f"| Sharpe (annualised, all-time) | {self._fmt_optional(self.sharpe)} |",
            f"| Sortino (annualised, all-time) | {self._fmt_optional(self.sortino)} |",
            "",
        ]
        if self.notes:
            lines.append("## Notes")
            for n in self.notes:
                lines.append(f"- {n}")
            lines.append("")
        return "\n".join(lines)


def generate_report(
    weeks: int = 1,
    now: datetime | None = None,
    trade_log: Path | str | None = None,
) -> WeeklyReport:
    """
    Load ``trades.jsonl`` for the trailing ``weeks`` period and aggregate tallies.

    Missing or empty logs yield an empty report with an explanatory note (no raise).
    """
    end = _utc_now(now)
    start = end - timedelta(weeks=max(1, weeks))
    week_start = start.date().isoformat()
    week_end = end.date().isoformat()

    if trade_log is None:
        log_path = _default_data_dir() / "trades.jsonl"
    else:
        log_path = Path(trade_log)

    report = WeeklyReport(week_start=week_start, week_end=week_end)

    if not log_path.is_file():
        report.notes.append(
            f"No trade log at `{log_path}` — returning empty aggregates."
        )
        return report

    in_window = 0
    for obj in _iter_trade_records(log_path):
        ts = _parse_ts(obj)
        if ts is None or ts < start or ts > end:
            continue
        in_window += 1
        report.trades_total += 1
        st = _status(obj)

        if st in ("filled", "fill", "filled_partial", "partial_fill"):
            report.trades_filled += 1
        elif st in ("cancelled", "canceled", "cancel"):
            report.trades_cancelled += 1
        elif not st and any(
            k in obj for k in ("side", "size", "price", "market_id", "token_id")
        ):
            # Rows that look like executions but omit explicit status
            report.trades_filled += 1

        for key in ("realized_pnl_usdc", "realized_pnl", "pnl_usdc", "pnl"):
            if key in obj and obj[key] is not None:
                try:
                    report.realized_pnl_usdc += float(obj[key])
                except (TypeError, ValueError):
                    pass
                break

        for key in ("unrealized_pnl_usdc", "unrealized_pnl"):
            if key in obj and obj[key] is not None:
                try:
                    report.unrealized_pnl_usdc += float(obj[key])
                except (TypeError, ValueError):
                    pass
                break

        fee = None
        for key in ("fees_paid_usdc", "fee_usdc", "fee", "fees_usd"):
            if key in obj and obj[key] is not None:
                try:
                    fee = float(obj[key])
                except (TypeError, ValueError):
                    fee = None
                break
        if fee is not None:
            report.fees_paid_usdc += fee

    if in_window == 0 and log_path.stat().st_size > 0:
        report.notes.append(
            "Trade log exists but no rows fell in the selected time window "
            "(check timestamps / machine clock)."
        )
    elif in_window == 0:
        report.notes.append("Trade log is empty for this window.")

    # Sharpe / Sortino: computed over the ENTIRE trades.jsonl history
    # (not the trailing window — a 7-day window cannot satisfy the
    # ≥20-daily-returns gate enforced by utils/risk_metrics.compute_sharpe
    # / compute_sortino). Returns None when gated; rendered as
    # "n/a (insufficient data)" in markdown. Imports are function-local
    # to preserve scripts/weekly_report.py's side-effect-free-on-import
    # contract (module docstring line 5).
    from utils.derived_stats import build_daily_equity_series_from_trades
    from utils.risk_metrics import compute_sharpe, compute_sortino

    series = build_daily_equity_series_from_trades(path=log_path)
    report.sharpe = compute_sharpe(series)
    report.sortino = compute_sortino(series)

    return report


def write_report(
    report: WeeklyReport,
    fmt: str = "md",
    out_dir: Path | str | None = None,
) -> Path:
    """Write ``report`` to ``BOT_REPORT_DIR`` (or ``out_dir``) as markdown or JSON."""
    base = Path(out_dir) if out_dir is not None else _default_report_dir()
    base.mkdir(parents=True, exist_ok=True)
    stamp = report.week_end.replace("-", "")
    if fmt.lower() in ("md", "markdown"):
        path = base / f"weekly_report_{stamp}.md"
        path.write_text(report.to_markdown(), encoding="utf-8")
        return path
    if fmt.lower() == "json":
        path = base / f"weekly_report_{stamp}.json"
        path.write_text(
            json.dumps(report.to_dict(), indent=2) + "\n",
            encoding="utf-8",
        )
        return path
    raise ValueError(f"Unsupported format: {fmt}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Weekly trade report from trades.jsonl")
    parser.add_argument("--weeks", type=int, default=1, help="Trailing weeks to include")
    parser.add_argument(
        "--format",
        choices=("md", "json"),
        default="md",
        help="Output format when writing to disk",
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="Print report to stdout instead of only writing file",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    if args.verbose:
        print(f"BOT_DATA_DIR={os.environ.get('BOT_DATA_DIR', 'data')}", file=sys.stderr)

    report = generate_report(weeks=args.weeks)
    out_path = write_report(report, fmt=args.format)

    if args.stdout:
        if args.format == "json":
            print(json.dumps(report.to_dict(), indent=2))
        else:
            print(report.to_markdown())

    if args.verbose:
        print(f"Wrote: {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
