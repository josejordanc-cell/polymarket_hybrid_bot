#!/usr/bin/env python3
"""
scripts/clear_sim_positions.py
v0.2.9 3h companion to scripts/inject_sim_position.py.

POSTs to a running bot's POST /debug/clear-positions endpoint to
empty both BotState.open_positions and BotState.recent_fills so the
next /dashboard/snapshot ships empty positions/fills. Same sim-mode +
Bearer-auth + loopback constraints as inject_sim_position.py.

Example:
  python scripts/clear_sim_positions.py
"""
from __future__ import annotations

import argparse
import os
import sys
import urllib.error
import urllib.request


_DEFAULT_URL = "http://127.0.0.1:8080/debug/clear-positions"


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "POST to a running bot's /debug/clear-positions endpoint "
            "(sim-mode only)."
        )
    )
    parser.add_argument("--url", default=_DEFAULT_URL)
    args = parser.parse_args()

    token = os.environ.get("BOT_HTTP_API_BOT_API_TOKEN")
    if not token:
        print(
            "ERROR: BOT_HTTP_API_BOT_API_TOKEN not set in environment.",
            file=sys.stderr,
        )
        sys.exit(2)

    req = urllib.request.Request(
        args.url,
        data=b"",
        headers={"Authorization": f"Bearer {token}"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            print(f"HTTP {resp.status} {resp.read().decode('utf-8')}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"HTTP {e.code} {body}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"URLError: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
