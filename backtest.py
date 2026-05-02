"""
backtest.py
Historical replay backtester for the hybrid strategy suite.

Usage:
    python backtest.py --data snapshots.jsonl --start 2025-01-01 --end 2025-03-31

Data format (JSONL, one JSON object per line):
    {
        "ts": "2025-01-15T10:00:00Z",
        "market_id": "0xABC...",
        "yes_bid": 0.45, "yes_ask": 0.46,
        "no_bid": 0.53, "no_ask": 0.54,
        "liquidity_usd": 12000,
        "tick_size": 0.01,
        "yes_token_id": "...", "no_token_id": "...",
        "spot_btc": 65000.0
    }
"""
from __future__ import annotations

import argparse
import asyncio
import json
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import structlog

from config.settings import get_config
from risk_manager import RiskManager
from simulator import PaperSimulator
from strategies.intra_arbitrage import IntraArbitrageStrategy
from strategies.market_maker import MarketMakerStrategy
from strategies.momentum_latency import MomentumLatencyStrategy
from utils.fill_correlation import record_fill_return_for_correlation
from utils.logging import configure_logging
from utils.types import MarketSnapshot, OrderIntent

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Backtest engine
# ---------------------------------------------------------------------------

class BacktestEngine:
    def __init__(self, start: datetime | None = None, end: datetime | None = None) -> None:
        self._start = start
        self._end = end
        configure_logging("INFO")

        self._config = get_config()
        self._risk = RiskManager(self._config)
        self._sim = PaperSimulator(slippage_bps=self._config.runtime.sim_slippage_bps)

        self._arb = IntraArbitrageStrategy(self._config)
        self._mom = MomentumLatencyStrategy(self._config)
        self._mm = MarketMakerStrategy(self._config)

        # Rolling spot price history per symbol (for z-score)
        self._spot_history: dict[str, deque[float]] = defaultdict(
            lambda: deque(maxlen=500)
        )

        # Per-market equity curve: list of (ts, equity) tuples
        self.equity_curve: list[tuple[str, float]] = []

    def _make_snapshot(self, row: dict) -> MarketSnapshot:
        return MarketSnapshot(
            market_id=row["market_id"],
            yes_bid=float(row.get("yes_bid", 0.45)),
            yes_ask=float(row.get("yes_ask", 0.46)),
            no_bid=float(row.get("no_bid", 0.53)),
            no_ask=float(row.get("no_ask", 0.54)),
            liquidity_usd=float(row.get("liquidity_usd", 10_000)),
            tick_size=float(row.get("tick_size", 0.01)),
            yes_token_id=row.get("yes_token_id", ""),
            no_token_id=row.get("no_token_id", ""),
        )

    def _get_return_series(self, symbol: str) -> np.ndarray:
        prices = np.asarray(self._spot_history[symbol], dtype=float)
        if len(prices) < 2:
            return np.array([])
        log_rets = np.diff(np.log(np.clip(prices, 1e-9, None)))
        return log_rets

    async def process_row(self, row: dict) -> None:
        ts_str = row.get("ts", "")
        snap = self._make_snapshot(row)

        # Update spot history
        for key in ("spot_btc", "spot_eth", "spot_sol"):
            sym_map = {"spot_btc": "BTC/USDT", "spot_eth": "ETH/USDT", "spot_sol": "SOL/USDT"}
            if key in row and row[key]:
                self._spot_history[sym_map[key]].append(float(row[key]))

        if self._risk.kill_switch_active():
            return

        intents: list[OrderIntent] = []

        # Priority 1: Arb
        arb_sig = self._arb.find_signal(snap)
        if arb_sig:
            size_usd = self._risk.kelly_size_usd(arb_sig)
            if size_usd > 0:
                intents = self._arb.build_orders(snap, size_usd)

        if not intents:
            # Priority 2: Momentum
            sym = "BTC/USDT"  # simplification; extend with symbol_map in production
            ret_series = self._get_return_series(sym)
            mom_sig = self._mom.find_signal(snap, sym, ret_series)
            if mom_sig:
                size_usd = self._risk.kelly_size_usd(mom_sig)
                order = self._mom.build_order(mom_sig, size_usd, snap)
                if order:
                    intents = [order]

        if not intents:
            # Priority 3: MM
            intents = self._mm.build_quotes(snap, self._config.risk.bankroll_usd * 0.01)

        approved = self._risk.approve_orders(intents)
        for intent in approved:
            result = await self._sim.execute(intent)
            if result.accepted and result.filled_size > 0:
                self._risk.on_fill(
                    intent,
                    fill_price=result.fill_price or intent.price,
                    filled_size=result.filled_size,
                )
                from observability.metrics_exporter import notify_fill_metrics

                notify_fill_metrics(
                    intent.strategy,
                    intent.side,
                    intent.tif,
                    result.fill_price,
                    result.filled_size,
                )
                record_fill_return_for_correlation(
                    self._risk,
                    intent,
                    snap,
                    result.fill_price,
                    result.filled_size,
                )

        self.equity_curve.append((ts_str, round(self._risk.equity, 2)))

    def print_summary(self) -> None:
        fills = self._sim.get_fill_log()
        if not fills:
            print("No fills generated.")
            return

        notionals = [f["notional_usd"] for f in fills]
        total_notional = sum(notionals)
        print(f"\n=== Backtest Summary ===")
        print(f"Total trades:       {len(fills)}")
        print(f"Total notional:     ${total_notional:,.2f}")
        print(f"Avg trade size:     ${total_notional / len(fills):,.2f}")
        print(f"Final equity:       ${self._risk.equity:,.2f}")
        start_eq = self._config.risk.bankroll_usd
        pnl = self._risk.equity - start_eq
        print(f"P&L:                ${pnl:,.2f} ({pnl / start_eq * 100:.2f}%)")
        print(f"\nBy strategy:")
        for strat, count in self._sim.summary().get("by_strategy", {}).items():
            print(f"  {strat:<25} {count} trades")
        print()


async def run_backtest(data_path: Path, start: str | None, end: str | None) -> None:
    start_dt = datetime.fromisoformat(start).replace(tzinfo=timezone.utc) if start else None
    end_dt = datetime.fromisoformat(end).replace(tzinfo=timezone.utc) if end else None

    engine = BacktestEngine(start=start_dt, end=end_dt)
    rows_processed = 0

    with data_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue

            # Date filtering
            ts_str = row.get("ts", "")
            if ts_str and (start_dt or end_dt):
                try:
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    if start_dt and ts < start_dt:
                        continue
                    if end_dt and ts > end_dt:
                        continue
                except ValueError:
                    pass

            await engine.process_row(row)
            rows_processed += 1

    logger.info("backtest_complete", rows=rows_processed)
    engine.print_summary()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backtest the hybrid strategy suite")
    parser.add_argument("--data", required=True, help="Path to JSONL data file")
    parser.add_argument("--start", help="Start date ISO 8601 (e.g. 2025-01-01)")
    parser.add_argument("--end", help="End date ISO 8601 (e.g. 2025-03-31)")
    args = parser.parse_args()
    asyncio.run(run_backtest(Path(args.data), args.start, args.end))

