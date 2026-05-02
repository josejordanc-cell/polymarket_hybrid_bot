"""
Standalone CLI for Polygon USDC vs bot equity (Prompt 9).

Prefer `bot_equity_usd` from the bot's /metrics endpoint; optionally fall back to
JSON log scrape when ``--no-metrics`` is set or the metrics HTTP request fails.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

from utils.equity_reconciliation import (
    DriftEvaluation,
    fetch_bot_equity_from_metrics,
    fetch_polygon_usdc_human,
    get_polygon_wallet_address,
    maybe_alert_equity_drift,
    scrape_latest_equity_from_log_file,
)


def _build_web3(rpc_url: str):
    from web3 import Web3

    return Web3(Web3.HTTPProvider(rpc_url))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Reconcile Polygon wallet USDC (bridged and/or native) against bot equity. "
            "By default reads bot_equity_usd from http://127.0.0.1:BOT_METRICS_PORT/metrics "
            "(Prometheus text format). If that fails or you pass --no-metrics, falls back "
            "to scraping the latest equity from a JSON structlog file (--log-file)."
        ),
    )
    parser.add_argument(
        "--alert",
        action="store_true",
        help="Send Telegram/Discord alert if |drift| exceeds threshold",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON instead of Markdown",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=None,
        help="Drift threshold in percent (overrides BOT_RECONCILIATION_DRIFT_THRESHOLD_PCT)",
    )
    parser.add_argument(
        "--no-metrics",
        action="store_true",
        help="Skip /metrics and use log scrape only (see --log-file)",
    )
    parser.add_argument(
        "--log-file",
        default=os.environ.get("BOT_LOG_PATH", "bot.log"),
        help="Structlog JSON file for equity fallback (default: BOT_LOG_PATH or ./bot.log)",
    )
    parser.add_argument(
        "--metrics-host",
        default=os.environ.get("BOT_METRICS_HOST", "127.0.0.1"),
        help="Host for /metrics (default BOT_METRICS_HOST or 127.0.0.1)",
    )
    args = parser.parse_args(argv)

    os.environ.setdefault("BOT_MODE", "sim")
    from config import get_config

    get_config.cache_clear()
    config = get_config()

    rpc = (config.api.polygon_rpc_url or "").strip()
    if not rpc:
        print("BOT_POLYGON_RPC_URL is required for on-chain balance", file=sys.stderr)
        return 2

    threshold = (
        float(args.threshold)
        if args.threshold is not None
        else float(config.reconciliation_drift_threshold_pct)
    )

    try:
        wallet = get_polygon_wallet_address(config)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    w3 = _build_web3(rpc)
    try:
        on_chain, usdc_src = fetch_polygon_usdc_human(w3, wallet)
    except Exception as exc:
        print(f"RPC error: {exc}", file=sys.stderr)
        return 2

    equity_source = "metrics"
    bot_equity: float | None = None
    port = int(config.metrics_port)

    if not args.no_metrics:
        bot_equity = fetch_bot_equity_from_metrics(
            port,
            config.experiment_id,
            config.runtime.mode,
            host=args.metrics_host,
        )

    if bot_equity is None:
        equity_source = "logs"
        bot_equity = scrape_latest_equity_from_log_file(args.log_file)
        if bot_equity is None:
            print(
                "Could not read bot equity: /metrics unreachable or missing bot_equity_usd, "
                f"and no equity found in {args.log_file!r}",
                file=sys.stderr,
            )
            return 2

    ev = DriftEvaluation.from_balances(on_chain, bot_equity, threshold)
    out: dict[str, Any] = {
        "wallet": wallet,
        "on_chain_usdc": round(on_chain, 6),
        "usdc_balance_source": usdc_src,
        "bot_equity_usd": round(bot_equity, 6),
        "equity_source": equity_source,
        "drift_usd": round(ev.drift_usd, 6),
        "drift_pct": round(ev.drift_pct, 6),
        "threshold_pct": threshold,
        "ok": not ev.breaches_threshold,
    }

    if args.json:
        print(json.dumps(out, indent=2))
    else:
        status = "OK" if not ev.breaches_threshold else "BREACH"
        print(f"## Wallet reconcile ({status})\n")
        print(f"- **Wallet**: `{wallet}`")
        print(f"- **On-chain USDC**: ${on_chain:,.2f} ({usdc_src})")
        print(f"- **Bot equity** ({equity_source}): ${bot_equity:,.2f}")
        print(f"- **Drift**: ${ev.drift_usd:+,.2f} ({ev.drift_pct:+.3f}%)")
        print(f"- **Threshold**: ±{threshold}%")

    if ev.breaches_threshold and args.alert:

        async def _go() -> None:
            from utils.alerts import send_alert

            await maybe_alert_equity_drift(
                drift_pct=ev.drift_pct,
                drift_usd=ev.drift_usd,
                on_chain_usdc=on_chain,
                bot_equity_usd=bot_equity,
                threshold_pct=threshold,
                send_alert=send_alert,
            )

        asyncio.run(_go())

    return 0 if not ev.breaches_threshold else 1
