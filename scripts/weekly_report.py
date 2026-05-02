#!/usr/bin/env python3
"""
Weekly performance report from structlog JSON lines (Prompt 7).

Reads logs only — no Prometheus or external APIs.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from glob import glob
from pathlib import Path
from typing import Any, Iterator, TextIO

# Align with risk_manager.POLYMARKET_FEE_PER_SIDE (avoid importing runtime stack).
POLYMARKET_FEE_PER_SIDE = 0.002

ISO_HELP = "UTC day or datetime, e.g. 2026-04-26 or 2026-04-26T00:00:00Z"


def _parse_start_instant_utc(s: str) -> datetime:
    raw = s.strip()
    if len(raw) == 10 and raw[4] == "-" and raw[7] == "-":
        raw = raw + "T00:00:00+00:00"
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_end_instant_utc(s: str) -> datetime:
    """Date-only end is inclusive through end of that UTC day."""
    raw = s.strip()
    if len(raw) == 10 and raw[4] == "-" and raw[7] == "-":
        day = datetime.fromisoformat(raw).replace(tzinfo=timezone.utc)
        end = day + timedelta(days=1) - timedelta(microseconds=1)
        return end
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _event_ts(obj: dict[str, Any]) -> datetime | None:
    for key in ("timestamp", "time", "@timestamp"):
        v = obj.get(key)
        if isinstance(v, str):
            try:
                return _parse_start_instant_utc(v)
            except ValueError:
                continue
    return None


def _iter_log_paths(pattern: str) -> list[Path]:
    paths = sorted({Path(p) for p in glob(pattern, recursive=True)})
    return [p for p in paths if p.is_file()]


def _iter_jsonl_lines(paths: list[Path]) -> Iterator[tuple[Path, int, dict[str, Any]]]:
    for path in paths:
        with path.open(encoding="utf-8", errors="replace") as f:
            for lineno, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    yield path, lineno, obj


def _fee_usd(fill_price: float, size: float, fee_usd: float | None) -> float:
    if fee_usd is not None:
        return float(fee_usd)
    return float(fill_price) * float(size) * POLYMARKET_FEE_PER_SIDE


def _fill_cashflow_usd(side: str, fill_price: float, size: float, fee_usd: float | None) -> float:
    cost = float(fill_price) * float(size)
    fee = _fee_usd(fill_price, size, fee_usd)
    if side.upper() == "BUY":
        return -(cost + fee)
    return cost - fee


def _market_prefix(m: str | None, n: int = 12) -> str:
    if not m:
        return ""
    return m[:n] if len(m) >= n else m


@dataclass
class _PendingFill:
    market_prefix: str
    outcome: str
    fill_price: float
    size: float
    fee_usd: float | None
    side: str


@dataclass
class _ArbLegPair:
    market_prefix: str
    yes: _PendingFill
    no: _PendingFill


@dataclass
class _BundleAgg:
    bundle_id: str
    leg_yes_pnl: float
    leg_no_pnl: float
    leg_yes_notional: float
    leg_no_notional: float
    unwind_pnl: float = 0.0
    unwind_notional: float = 0.0
    unwind_skipped_notes: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class _StrategyAgg:
    fill_count: int = 0
    notional_usd: float = 0.0
    trade_pnls: list[float] = field(default_factory=list)


def _stderr(msg: str) -> None:
    print(msg, file=sys.stderr)


def _collect_experiment_ids_in_window(
    paths: list[Path], start: datetime, end: datetime
) -> list[str]:
    found: set[str] = set()
    for _, _, obj in _iter_jsonl_lines(paths):
        ts = _event_ts(obj)
        if ts is None or ts < start or ts > end:
            continue
        eid = obj.get("experiment_id")
        if isinstance(eid, str) and eid:
            found.add(eid)
        elif eid is None:
            found.add("(missing)")
    return sorted(found)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Weekly bot performance report from JSON logs (structlog)."
    )
    p.add_argument("--logs", required=True, help="Log file path or glob (e.g. logs/*.jsonl)")
    p.add_argument("--start", required=True, help=f"Window start ({ISO_HELP})")
    p.add_argument("--end", required=True, help=f"Window end ({ISO_HELP}; date-only is inclusive)")
    p.add_argument(
        "--experiment-id",
        default=None,
        help="Required: experiment id bound in structlog context (omit to list available ids and exit).",
    )
    p.add_argument("--format", choices=("markdown", "json"), default="markdown")
    return p.parse_args(argv)


def build_report(
    lines: Iterator[tuple[Any, int, dict[str, Any]]],
    start: datetime,
    end: datetime,
    experiment_id: str,
) -> dict[str, Any]:
    arb_yes_wait: dict[str, deque[_PendingFill]] = defaultdict(deque)

    modes_seen: list[str] = []
    equity_series: list[float] = []

    fill_backlog: deque[_PendingFill] = deque()

    arb_pair_queue: deque[_ArbLegPair] = deque()
    arb_completion_queue: deque[tuple[str, str, dict[str, Any]]] = deque()

    bundles: dict[str, _BundleAgg] = {}
    unwind_pnl_by_bundle: dict[str, float] = defaultdict(float)
    unwind_notional_by_bundle: dict[str, float] = defaultdict(float)
    unwind_skipped_global: list[dict[str, Any]] = []

    strategies: dict[str, _StrategyAgg] = {
        "intra_arbitrage": _StrategyAgg(),
        "momentum_latency": _StrategyAgg(),
        "market_maker": _StrategyAgg(),
        "smart_money_copy": _StrategyAgg(),
    }

    incidents = {
        "polymarket_ws_closed": 0,
        "live_order_rejected": 0,
        "kill_switch_triggered": 0,
    }
    market_resolved = 0

    trade_count = 0
    orphan_intra_pnls: list[float] = []
    orphan_intra_notional: list[float] = []

    for _path, _lineno, obj in lines:
        ts = _event_ts(obj)
        if ts is None or ts < start or ts > end:
            continue
        eid = obj.get("experiment_id")
        if not (isinstance(eid, str) and eid == experiment_id):
            continue

        ev = obj.get("event")
        if not isinstance(ev, str):
            continue

        if ev == "bot_starting":
            m = obj.get("mode")
            if isinstance(m, str):
                modes_seen.append(m)

        if ev == "fill_recorded":
            eq = obj.get("equity")
            if isinstance(eq, (int, float)):
                equity_series.append(float(eq))
            side = str(obj.get("side") or "BUY")
            fp = obj.get("fill_price")
            sz = obj.get("size")
            outcome = str(obj.get("outcome") or "")
            market = str(obj.get("market") or "")
            if isinstance(fp, (int, float)) and isinstance(sz, (int, float)):
                fee = obj.get("fee_usd")
                fee_v = float(fee) if isinstance(fee, (int, float)) else None
                fill_backlog.append(
                    _PendingFill(
                        market_prefix=_market_prefix(market),
                        outcome=outcome,
                        fill_price=float(fp),
                        size=float(sz),
                        fee_usd=fee_v,
                        side=side,
                    )
                )

        if ev == "trade_result":
            st = obj.get("strategy")
            if not isinstance(st, str):
                continue
            acc = obj.get("accepted")
            fp = obj.get("fill_price")
            sz = obj.get("size")
            if acc is not True or not isinstance(fp, (int, float)) or not isinstance(
                sz, (int, float)
            ):
                continue
            if float(sz) <= 0:
                continue
            trade_count += 1
            mpre = str(obj.get("market") or "")
            oc = str(obj.get("outcome") or "")

            matched: _PendingFill | None = None
            for _ in range(len(fill_backlog)):
                cand = fill_backlog.popleft()
                if (
                    cand.market_prefix == mpre
                    and cand.outcome == oc
                    and abs(cand.fill_price - float(fp)) < 1e-9
                    and abs(cand.size - float(sz)) < 1e-9
                ):
                    matched = cand
                    break
                fill_backlog.append(cand)
            if matched is None:
                matched = _PendingFill(
                    market_prefix=mpre,
                    outcome=oc,
                    fill_price=float(fp),
                    size=float(sz),
                    fee_usd=None,
                    side="BUY",
                )

            cf = _fill_cashflow_usd(
                matched.side, matched.fill_price, matched.size, matched.fee_usd
            )
            notional = abs(float(fp) * float(sz))

            if st == "intra_arbitrage":
                if oc == "YES":
                    arb_yes_wait[mpre].append(matched)
                elif oc == "NO":
                    qy = arb_yes_wait.get(mpre)
                    if qy and qy:
                        yes_fill = qy.popleft()
                        arb_pair_queue.append(
                            _ArbLegPair(
                                market_prefix=mpre,
                                yes=yes_fill,
                                no=matched,
                            )
                        )
                    else:
                        orphan_intra_pnls.append(cf)
                        orphan_intra_notional.append(notional)
                else:
                    orphan_intra_pnls.append(cf)
                    orphan_intra_notional.append(notional)
            elif st in strategies:
                agg = strategies[st]
                agg.fill_count += 1
                agg.notional_usd += notional
                agg.trade_pnls.append(cf)
            else:
                na = strategies.setdefault(st, _StrategyAgg())
                na.fill_count += 1
                na.notional_usd += notional
                na.trade_pnls.append(cf)

        if ev == "arb_bundle_balanced":
            bid = obj.get("bundle_id")
            if isinstance(bid, str) and bid:
                arb_completion_queue.append(("balanced", bid, dict(obj)))

        if ev == "arb_unwind":
            bid = obj.get("bundle_id")
            if isinstance(bid, str) and bid:
                arb_completion_queue.append(("unwind", bid, dict(obj)))

        if ev == "arb_unwind_skipped":
            bid = obj.get("bundle_id")
            if isinstance(bid, str) and bid:
                arb_completion_queue.append(("unwind_skipped", bid, dict(obj)))
                unwind_skipped_global.append(dict(obj))

        if ev == "arb_unwind_fill_recorded":
            bid = obj.get("bundle_id")
            fp = obj.get("fill_price")
            sz = obj.get("filled_size")
            if (
                isinstance(bid, str)
                and bid
                and isinstance(fp, (int, float))
                and isinstance(sz, (int, float))
            ):
                fee_v = _fee_usd(float(fp), float(sz), None)
                unwind_cf = _fill_cashflow_usd("SELL", float(fp), float(sz), fee_v)
                unwind_pnl_by_bundle[bid] += unwind_cf
                unwind_notional_by_bundle[bid] += abs(float(fp) * float(sz))

        if ev == "polymarket_ws_closed":
            incidents["polymarket_ws_closed"] += 1
        elif ev == "live_order_rejected":
            incidents["live_order_rejected"] += 1
        elif ev == "kill_switch_triggered":
            incidents["kill_switch_triggered"] += 1
        elif ev == "market_resolved":
            market_resolved += 1

    while arb_pair_queue and arb_completion_queue:
        pair = arb_pair_queue.popleft()
        kind, bid, raw = arb_completion_queue.popleft()
        leg_yes_cf = _fill_cashflow_usd(
            pair.yes.side, pair.yes.fill_price, pair.yes.size, pair.yes.fee_usd
        )
        leg_no_cf = _fill_cashflow_usd(
            pair.no.side, pair.no.fill_price, pair.no.size, pair.no.fee_usd
        )
        yn = abs(pair.yes.fill_price * pair.yes.size)
        nn = abs(pair.no.fill_price * pair.no.size)
        b = _BundleAgg(
            bundle_id=bid,
            leg_yes_pnl=leg_yes_cf,
            leg_no_pnl=leg_no_cf,
            leg_yes_notional=yn,
            leg_no_notional=nn,
        )
        if kind == "unwind_skipped":
            b.unwind_skipped_notes.append(raw)
        bundles[bid] = b

    # Unwind fills are logged after arb_unwind / completion; apply after full scan.
    for b in bundles.values():
        b.unwind_pnl = unwind_pnl_by_bundle.get(b.bundle_id, 0.0)
        b.unwind_notional = unwind_notional_by_bundle.get(b.bundle_id, 0.0)

    mode = modes_seen[0] if modes_seen else "unknown"

    arb_bundles = list(bundles.values())
    arb_bundle_pnls: list[float] = []
    for b in arb_bundles:
        total = b.leg_yes_pnl + b.leg_no_pnl + b.unwind_pnl
        arb_bundle_pnls.append(total)

    arb_wins = sum(1 for p in arb_bundle_pnls if p > 0)
    arb_win_rate = (arb_wins / len(arb_bundle_pnls)) if arb_bundle_pnls else 0.0

    arb_agg = strategies["intra_arbitrage"]
    arb_agg.fill_count = sum(
        2 + (1 if abs(b.unwind_pnl) > 1e-12 else 0) for b in arb_bundles
    )
    arb_agg.fill_count += len(orphan_intra_pnls)
    arb_agg.notional_usd = sum(
        b.leg_yes_notional + b.leg_no_notional + b.unwind_notional for b in arb_bundles
    )
    arb_agg.notional_usd += sum(orphan_intra_notional)
    arb_agg.trade_pnls = list(arb_bundle_pnls)

    def _mean(xs: list[float]) -> float:
        return sum(xs) / len(xs) if xs else 0.0

    def _win_rate(xs: list[float]) -> float:
        if not xs:
            return 0.0
        return sum(1 for x in xs if x > 0) / len(xs)

    arb_win_rate = (arb_wins / len(arb_bundle_pnls)) if arb_bundle_pnls else 0.0
    arb_mean_on_bundles = _mean(arb_bundle_pnls)

    starting_equity = equity_series[0] if equity_series else None
    ending_equity = equity_series[-1] if equity_series else None
    total_pnl_usd: float | None = None
    total_pnl_pct: float | None = None
    if starting_equity is not None and ending_equity is not None:
        total_pnl_usd = ending_equity - starting_equity
        if abs(starting_equity) > 1e-9:
            total_pnl_pct = 100.0 * total_pnl_usd / starting_equity

    max_dd_pct = 0.0
    if equity_series:
        peak = equity_series[0]
        for x in equity_series:
            if x > peak:
                peak = x
            dd = (peak - x) / peak * 100.0 if peak > 0 else 0.0
            if dd > max_dd_pct:
                max_dd_pct = dd

    arb_total = sum(arb_bundle_pnls) + sum(orphan_intra_pnls)
    mom = strategies["momentum_latency"]
    mm = strategies["market_maker"]
    sm = strategies["smart_money_copy"]

    strategy_totals: dict[str, float] = {
        "intra_arbitrage": arb_total,
        "momentum_latency": sum(mom.trade_pnls),
        "market_maker": sum(mm.trade_pnls),
        "smart_money_copy": sum(sm.trade_pnls),
    }
    for k, agg in strategies.items():
        if k not in strategy_totals and agg.trade_pnls:
            strategy_totals[k] = sum(agg.trade_pnls)

    best = max(strategy_totals, key=lambda k: strategy_totals[k])

    mm_horizon_equity_delta: float | None = None
    if starting_equity is not None and ending_equity is not None:
        mm_horizon_equity_delta = ending_equity - starting_equity

    other_strategies = {
        k: {
            "fill_count": v.fill_count,
            "notional_usd": round(v.notional_usd, 2),
            "mean_pnl_per_trade_usd": round(_mean(v.trade_pnls), 6),
            "win_rate": round(_win_rate(v.trade_pnls), 4),
            "total_pnl_usd": round(sum(v.trade_pnls), 4),
        }
        for k, v in strategies.items()
        if k
        not in (
            "intra_arbitrage",
            "momentum_latency",
            "market_maker",
            "smart_money_copy",
        )
    }

    return {
        "period_utc": {
            "start": start.isoformat().replace("+00:00", "Z"),
            "end": end.isoformat().replace("+00:00", "Z"),
        },
        "experiment_id": experiment_id,
        "mode": mode,
        "starting_equity_usd": starting_equity,
        "ending_equity_usd": ending_equity,
        "total_pnl_usd": total_pnl_usd,
        "total_pnl_pct": total_pnl_pct,
        "trade_count": trade_count,
        "strategies": {
            "intra_arbitrage": {
                "bundles_completed": len(arb_bundles),
                "fill_count": arb_agg.fill_count,
                "notional_usd": round(arb_agg.notional_usd, 2),
                "mean_pnl_per_trade_usd": round(arb_mean_on_bundles, 6),
                "win_rate": round(arb_win_rate, 4),
                "total_pnl_usd": round(strategy_totals["intra_arbitrage"], 4),
                "notes": (
                    "Bundle PnL = leg YES cashflow + leg NO cashflow + unwind fill cashflow "
                    "(bundle_id from arb_unwind / arb_bundle_balanced / arb_unwind_skipped; "
                    "unwind fill from arb_unwind_fill_recorded)."
                ),
            },
            "momentum_latency": {
                "fill_count": mom.fill_count,
                "notional_usd": round(mom.notional_usd, 2),
                "mean_pnl_per_trade_usd": round(_mean(mom.trade_pnls), 6),
                "win_rate": round(_win_rate(mom.trade_pnls), 4),
                "total_pnl_usd": round(strategy_totals["momentum_latency"], 4),
            },
            "market_maker": {
                "fill_count": mm.fill_count,
                "notional_usd": round(mm.notional_usd, 2),
                "mean_pnl_per_trade_usd": round(_mean(mm.trade_pnls), 6),
                "win_rate": round(_win_rate(mm.trade_pnls), 4),
                "total_pnl_usd": round(strategy_totals["market_maker"], 4),
                "horizon_equity_delta_usd": mm_horizon_equity_delta,
            },
            "smart_money_copy": {
                "fill_count": sm.fill_count,
                "notional_usd": round(sm.notional_usd, 2),
                "mean_pnl_per_trade_usd": round(_mean(sm.trade_pnls), 6),
                "win_rate": round(_win_rate(sm.trade_pnls), 4),
                "total_pnl_usd": round(strategy_totals["smart_money_copy"], 4),
            },
        },
        "other_strategies": other_strategies,
        "intra_arbitrage_orphan_legs": {
            "count": len(orphan_intra_pnls),
            "total_pnl_usd": round(sum(orphan_intra_pnls), 6),
        },
        "max_intraweek_drawdown_pct": round(max_dd_pct, 4),
        "best_performer": best,
        "incidents": dict(incidents),
        "market_resolved_count": market_resolved,
        "arb_unwind_skipped_notes": unwind_skipped_global,
        "arb_unmatched_pairs": len(arb_pair_queue),
        "arb_unmatched_completions": len(arb_completion_queue),
    }


def _markdown_table(report: dict[str, Any]) -> str:
    p = report["period_utc"]
    week = f"{p['start'][:10]} – {p['end'][:10]} UTC"
    mode = report["mode"]
    se = report["starting_equity_usd"]
    ee = report["ending_equity_usd"]
    tp = report["total_pnl_usd"]
    tpp = report["total_pnl_pct"]

    def _money(x: Any) -> str:
        if x is None:
            return "N/A"
        return f"${x:,.2f}"

    tp_s = "N/A"
    if tp is not None and tpp is not None:
        sign = "+" if tp >= 0 else ""
        tp_s = f"{sign}{_money(tp)} ({sign}{tpp:.2f}%)"

    st = report["strategies"]
    arb = st["intra_arbitrage"]
    mom = st["momentum_latency"]
    mm = st["market_maker"]
    sm = st["smart_money_copy"]

    by_line = (
        f"`intra_arbitrage`: {arb['bundles_completed']} bundles "
        f"({arb['fill_count']} leg fills incl. unwinds); "
        f"`momentum_latency`: {mom['fill_count']}; "
        f"`market_maker`: {mm['fill_count']}"
    )
    if sm["fill_count"]:
        by_line += f"; `smart_money_copy`: {sm['fill_count']}"

    win_line = (
        f"Arb bundles {arb['win_rate']*100:.1f}% win (bundle PnL incl. unwind); "
        f"momentum {mom['win_rate']*100:.1f}%; "
        f"MM {mm['win_rate']*100:.1f}% (per-fill); MM horizon equity Δ {_money(mm.get('horizon_equity_delta_usd'))}"
    )

    lines = [
        "| Metric | Value |",
        "|--------|-------|",
        f"| Week | {week} |",
        f"| Mode | `{mode}` |",
        f"| Starting equity | {_money(se)} |",
        f"| Ending equity | {_money(ee)} |",
        f"| Total PnL | {tp_s} |",
        f"| Trades | {report['trade_count']} |",
        f"| By strategy (fills) | {by_line} |",
        f"| Win rate (simplified) | {win_line} |",
        f"| Max intraweek drawdown | {report['max_intraweek_drawdown_pct']:.2f}% |",
        f"| Best performer | `{report['best_performer']}` |",
        f"| Incidents | ws_closed={report['incidents']['polymarket_ws_closed']}, "
        f"live_rejected={report['incidents']['live_order_rejected']}, "
        f"kill_switch={report['incidents']['kill_switch_triggered']} |",
        "| **Recommendation** | *Apply the profitability rubric in `docs/HANDOFF.md` to total PnL and incidents above.* |",
    ]
    notes: list[str] = []
    skipped = report.get("arb_unwind_skipped_notes") or []
    if skipped:
        notes.append(
            "**Arb unwind skipped (unrealized / risk note, not counted in PnL):** "
            + "; ".join(
                f"`{s.get('bundle_id')}`: {s.get('reason')} (imbalance={s.get('imbalance_size')})"
                for s in skipped
            )
        )
    if report.get("arb_unmatched_pairs") or report.get("arb_unmatched_completions"):
        notes.append(
            f"**Warning:** unmatched arb pair/completion backlog "
            f"(pairs={report['arb_unmatched_pairs']}, completions={report['arb_unmatched_completions']})."
        )
    if report.get("market_resolved_count"):
        notes.append(f"**market_resolved events:** {report['market_resolved_count']}")

    out = "\n".join(lines)
    if notes:
        out += "\n\n" + "\n\n".join(notes)
    return out


def main(argv: list[str] | None = None, stdout: TextIO | None = None) -> int:
    args = _parse_args(argv)
    stdout = stdout or sys.stdout
    start = _parse_start_instant_utc(args.start)
    end = _parse_end_instant_utc(args.end)
    if end < start:
        _stderr("weekly_report: --end must be >= --start")
        return 2

    paths = _iter_log_paths(args.logs)
    if not paths:
        _stderr(f"No log files matched: {args.logs!r}")
        return 2

    if args.experiment_id is None:
        ids = _collect_experiment_ids_in_window(paths, start, end)
        listed = ", ".join(ids) if ids else "(none — no lines in window with experiment_id)"
        _stderr(
            "weekly_report: --experiment-id is required. "
            f"Found experiments in log window: {listed}. "
            "Pass --experiment-id to select one."
        )
        return 2

    lines_iter = _iter_jsonl_lines(paths)
    report = build_report(lines_iter, start, end, args.experiment_id)

    if args.format == "json":
        stdout.write(json.dumps(report, indent=2) + "\n")
    else:
        stdout.write(_markdown_table(report) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
