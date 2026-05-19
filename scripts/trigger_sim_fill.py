#!/usr/bin/env python3
"""
scripts/trigger_sim_fill.py
Used by the Phase E.2 smoke pass to verify B.1 FILL strategy-id
translation end-to-end against a running bot+dashboard pair.

NOT a backtesting tool. NOT a live-trading helper. Outputs to the
bot's WS broadcast channel only (via utils.fills.broadcast_fill).

Exit 0 even when no Emitter is registered — broadcast_fill is a
fire-and-forget that logs a warning when no subscribers exist.
The smoke runner is responsible for starting the bot first.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import structlog

from api.schemas import bot_to_dashboard
from utils.fills import broadcast_fill

logger = structlog.get_logger(__name__)

_SIDE_TO_ACTION = {
    "buy": "UP",
    "sell": "DOWN",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Emit a sim-mode fill to the bot's WS broadcast channel.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--strategy",
        default="intra_arbitrage",
        help="Internal strategy name (intra_arbitrage / momentum_latency / market_maker).",
    )
    parser.add_argument(
        "--venue",
        default="polymarket",
        help="Venue label (free-form string, for logging only).",
    )
    parser.add_argument(
        "--side",
        default="buy",
        choices=["buy", "sell"],
        help="Trade side.",
    )
    parser.add_argument(
        "--size-usd",
        dest="size_usd",
        default=100.0,
        type=float,
        help="Fill size in USD.",
    )
    parser.add_argument(
        "--price",
        default=0.5,
        type=float,
        help="Fill price (0.0–1.0 for binary market outcomes).",
    )
    parser.add_argument(
        "--token-id",
        dest="token_id",
        default="sim-token-0000000000000000",
        type=str,
        help="Fake token/market ID passed as market_id to broadcast_fill.",
    )
    return parser


async def _emit(args: argparse.Namespace) -> None:
    # Validate strategy mapping before attempting the broadcast so a bogus
    # name fails fast with a clear message.
    dashboard_id = bot_to_dashboard(args.strategy)

    action = _SIDE_TO_ACTION[args.side]

    await broadcast_fill(
        market_id=args.token_id,
        action=action,
        price=args.price,
        size=args.size_usd,
        strategy=args.strategy,
        pnl=None,
    )

    logger.info(
        "sim_fill_triggered",
        strategy_internal=args.strategy,
        strategy_dashboard=dashboard_id,
        venue=args.venue,
        side=args.side,
        size_usd=args.size_usd,
        price=args.price,
        token_id=args.token_id,
        action=action,
    )


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    # Validate strategy early so a bad value raises ValueError before we
    # attempt the broadcast.  bot_to_dashboard raises ValueError for unknown
    # names (e.g. smart_money_copy has no v1.0 contract id).
    bot_to_dashboard(args.strategy)

    asyncio.run(_emit(args))


if __name__ == "__main__":
    main()
