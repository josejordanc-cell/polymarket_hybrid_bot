"""
utils/settlements.py
Profitability epic PR2 — settlement-outcome ledger.

Appends one JSON line per market settlement to ``BOT_DATA_DIR/settlements.jsonl``.
The settlement-persistence half of PR2; pair with
``utils.derived_stats.compute_win_rate`` / ``compute_streak`` for the
read side that closes the last two ``GlobalStats`` zero-stubs
(``winRate``, ``streak`` at ``api/routes_rest.py:249,251``).

The PR2 recon confirmed the call site at ``main.py:113-116`` already
captures per-settlement pnl via the public realised-P&L accessor
``risk.daily_pnl_snapshot().realised_pnl_usd`` delta (non-lossy because
``risk.on_settlement`` is synchronous — no ``await`` between the two
reads, so asyncio cooperative scheduling cannot interleave another
task's ``risk._state`` mutation). This module is wired adjacent to
that existing computation and REUSES the existing ``pnl`` variable.

Locked policy (2026-05-26):

- **Reads use PUBLIC SURFACE ONLY.** Caller-side ``risk.equity``
  property reads at the wiring site; this module reads no fenced
  state. NO ``risk._state`` access anywhere.
- **Fire-and-forget.** Never raises. Failures are logged and
  swallowed, matching ``utils.trades.persist_fill`` /
  ``utils.fills.broadcast_fill`` / ``utils.alerts.send_alert``.
- **Append-only JSONL** at ``BOT_DATA_DIR/settlements.jsonl`` —
  parallel to ``BOT_DATA_DIR/trades.jsonl``. Resolution pattern is
  identical: ``Path(os.environ.get("BOT_DATA_DIR", "data"))`` (matches
  ``utils/trades.py:45`` verbatim).

Row schema (6 fields, locked):

  ts                ISO 8601 UTC, millisecond precision, 'Z' suffix
  market_id         Polymarket market_id (full)
  resolved_outcome  "YES" / "NO" — the winning outcome
  pnl               net round-trip pnl across both legs (caller's realised_pnl delta)
  equity_after      risk.equity AFTER on_settlement mutated it
  winning           bool — ``pnl > 0`` — explicit for readability
                    (technically derivable from pnl; included for clarity)

The ``winning`` field treats break-even settlements (``pnl == 0``) as
``False`` — neither a win nor a loss. ``compute_win_rate`` and
``compute_streak`` on the read side EXCLUDE break-evens from both
numerator and denominator (the read-side semantic is locked
separately; this field is just a fast-path explicit-readability
boolean derived from the pnl sign).
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


def _resolve_settlements_path() -> Path:
    """Return BOT_DATA_DIR/settlements.jsonl, mirroring
    utils.trades._resolve_trades_path at utils/trades.py:43-45."""
    return Path(os.environ.get("BOT_DATA_DIR", "data")) / "settlements.jsonl"


def persist_settlement(
    *,
    market_id: str,
    resolved_outcome: str,
    pnl: float,
    equity_after: float,
    path: Optional[Path] = None,
) -> None:
    """Append one JSON line to BOT_DATA_DIR/settlements.jsonl.

    Row schema (locked — see module docstring):

      ts                ISO 8601 UTC, millisecond precision, 'Z' suffix
      market_id         Polymarket market_id
      resolved_outcome  "YES" / "NO"
      pnl               net round-trip pnl (caller-supplied; realised_pnl delta)
      equity_after      risk.equity after on_settlement
      winning           bool, derived from pnl > 0

    Defensive: never raises. Logs to stderr via structlog on failure.
    Creates parent directory if missing.

    Args:
        path: test-only override. When None, resolves
              BOT_DATA_DIR/settlements.jsonl fresh on each call so
              env-var changes during a test session take effect.
    """
    try:
        target = path if path is not None else _resolve_settlements_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "ts": utc_now().strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(_time_mod.time() * 1000) % 1000:03d}Z",
            "market_id": market_id,
            "resolved_outcome": resolved_outcome,
            "pnl": float(pnl),
            "equity_after": float(equity_after),
            "winning": float(pnl) > 0.0,
        }
        with target.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, separators=(",", ":")) + "\n")
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "settlements_jsonl_write_failed",
            error=str(exc),
            market_id=market_id,
        )
