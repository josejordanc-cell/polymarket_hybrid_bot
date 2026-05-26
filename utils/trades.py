"""
utils/trades.py
Profitability epic piece (1) — persistent trade ledger.

Appends one JSON line per accepted fill to ``BOT_DATA_DIR/trades.jsonl``,
producing the input ``scripts/weekly_report.py`` has expected since
v0.2.1 (see ``HANDOFF.md:60``, ``.swarm-decisions.md``
``profitability-telemetry-gap``).

Schema is pinned (12 fields + 1 optional, ``order_id``); ``weekly_report.py``'s
flexible-alias reader subsumes it. See ``.swarm-decisions.md``
``b1-trades-writer-wiring`` for the locked schema decision (A1).

Module-level function — NOT a singleton. Unlike ``utils/fills.set_emitter``
/ ``utils/alerts.set_emitter``, there is no broadcast lifecycle to manage:
each call opens-appends-closes. Tests redirect output via the ``path=``
kwarg; no process-global state.

Fire-and-forget: never raises. Failures are logged and swallowed, matching
the contract of ``broadcast_fill`` and ``send_alert``.

``BOT_DATA_DIR`` resolution: raw ``os.environ.get`` mirroring
``scripts/weekly_report.py:50``. The Docker container sets
``BOT_DATA_DIR=/app/data`` explicitly (``docker-compose.yml``), landing
trades in the ``market_cache_data`` named volume for cross-rebuild
persistence.
"""
from __future__ import annotations

import json
import os
import time as _time_mod
from pathlib import Path
from typing import Optional

import structlog

from utils.types import utc_now

logger = structlog.get_logger(__name__)


def _resolve_trades_path() -> Path:
    """Return BOT_DATA_DIR/trades.jsonl, mirroring scripts/weekly_report.py."""
    return Path(os.environ.get("BOT_DATA_DIR", "data")) / "trades.jsonl"


def persist_fill(
    *,
    market_id: str,
    outcome: str,
    side: str,
    strategy: str,
    fill_price: float,
    filled_size: float,
    fee_usdc: float,
    equity_after: float,
    tif: Optional[str] = None,
    order_id: Optional[str] = None,
    path: Optional[Path] = None,
) -> None:
    """Append one JSON line to BOT_DATA_DIR/trades.jsonl.

    Row schema (locked A1 — see .swarm-decisions b1-trades-writer-wiring):

      ts           ISO 8601 UTC, millisecond precision, 'Z' suffix
      status       always "filled" at this call site (caller guards)
      market_id    Polymarket market_id (full)
      outcome      "YES" / "NO"
      side         "BUY" / "SELL"
      strategy     internal name ("intra_arbitrage", etc.) — NOT translated
      price        fill price
      size         filled size
      fee_usdc     post-fee deduction (POLYMARKET_FEE_PER_SIDE * price * size)
      equity_after risk.equity read AFTER risk.on_fill mutated it
      tif          optional, "IOC" / "GTC" / etc. at intent boundary
      order_id     optional, present for live-mode reconciled fills

    Defensive: never raises. Logs to stderr via structlog on failure.
    Creates parent directory if missing.

    Args:
        path: test-only override. When None, resolves BOT_DATA_DIR/trades.jsonl
              fresh on each call so env-var changes during a test session
              take effect.
    """
    try:
        target = path if path is not None else _resolve_trades_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "ts": utc_now().strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(_time_mod.time() * 1000) % 1000:03d}Z",
            "status": "filled",
            "market_id": market_id,
            "outcome": outcome,
            "side": side,
            "strategy": strategy,
            "price": float(fill_price),
            "size": float(filled_size),
            "fee_usdc": float(fee_usdc),
            "equity_after": float(equity_after),
            "tif": tif,
            "order_id": order_id,
        }
        with target.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, separators=(",", ":")) + "\n")
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "trades_jsonl_write_failed",
            error=str(exc),
            market_id=market_id,
            strategy=strategy,
        )
