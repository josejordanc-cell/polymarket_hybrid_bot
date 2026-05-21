#!/usr/bin/env python3
"""
scripts/inject_sim_position.py
v0.2.9 3h companion to scripts/trigger_sim_fill.py.

POSTs a synthetic Position into a running bot's BotState via the
sim-mode-only POST /debug/inject-position endpoint. The bot writes
both ``BotState.open_positions`` and ``BotState.recent_fills`` so the
next ``GET /dashboard/snapshot`` reflects the injected state.

Stdlib-only (no new deps): uses ``urllib.request`` rather than
``requests``.

Reads ``BOT_HTTP_API_BOT_API_TOKEN`` from the environment (matching the
.env variable name). The bot must be running in sim mode on
http://127.0.0.1:8080 (the loopback bind-host enforced by
config/settings.py:HttpApiConfig defaults).

Example:
  python scripts/inject_sim_position.py \\
    --market-id 0xabc \\
    --outcome YES \\
    --quantity 50 \\
    --avg-price 0.55 \\
    --strategy strategy1
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


_DEFAULT_URL = "http://127.0.0.1:8080/debug/inject-position"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "POST a synthetic Position to a running bot's "
            "/debug/inject-position endpoint (sim-mode only)."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--market-id", default="sim-mkt-0xabc")
    parser.add_argument("--outcome", default="YES", choices=["YES", "NO"])
    parser.add_argument("--quantity", default=50.0, type=float)
    parser.add_argument("--avg-price", default=0.55, type=float)
    parser.add_argument(
        "--strategy",
        default="strategy1",
        choices=["strategy1", "strategy2", "strategy3"],
        help="Wire-form strategy id.",
    )
    parser.add_argument(
        "--opened-at",
        default=None,
        help=(
            "ISO 8601 UTC string, e.g. '2026-05-21T13:00:00Z'. "
            "Omit to let the bot use datetime.now(timezone.utc)."
        ),
    )
    parser.add_argument(
        "--url",
        default=_DEFAULT_URL,
        help="Endpoint URL. Override only if bot is on a non-default host/port.",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()

    token = os.environ.get("BOT_HTTP_API_BOT_API_TOKEN")
    if not token:
        print(
            "ERROR: BOT_HTTP_API_BOT_API_TOKEN not set in environment.",
            file=sys.stderr,
        )
        sys.exit(2)

    payload: dict[str, object] = {
        "market_id": args.market_id,
        "outcome": args.outcome,
        "quantity": args.quantity,
        "avg_price": args.avg_price,
        "strategy": args.strategy,
    }
    if args.opened_at is not None:
        payload["opened_at"] = args.opened_at

    req = urllib.request.Request(
        args.url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = resp.read().decode("utf-8")
            print(f"HTTP {resp.status} {body}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"HTTP {e.code} {body}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"URLError: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
