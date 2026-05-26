"""
utils/derived_stats.py
Profitability epic PR1 — derived stats for ``GlobalStats``.

Snapshot-build-time lazy compute (matches B.1 RiskMetrics, B.7
equity_history, and ``monthlyPnl`` precedent). No background task, no
asyncio loop. Closes 3 of the 5 ``GlobalStats`` zero-stubs at
``api/routes_rest.py:247-251``:

- ``pnlMonth``  ← ``compute_pnl_month(equity_history)``
- ``pnlAllTime`` ← ``compute_pnl_all_time(equity_history)``
- ``totalTrades`` ← ``count_total_trades()``

The remaining two stubs (``winRate``, ``streak``) need closed-position
win/loss outcomes which require a settlement-persistence pipeline that
does not exist yet — DEFERRED to PR2. Those two stay as literal ``0.0``
/ ``0`` in this PR.

Data sources used (all available today; no fence touch):

- ``state.equity_history`` — live post-(2b) RiskManager → BotState
  polling mirror (commit ``8ff2236``). Bounded deque (``maxlen=200``)
  with one EquityPoint per UTC calendar day appended by
  ``utils.equity_curve.equity_snapshot_loop``.
- ``BOT_DATA_DIR/trades.jsonl`` — written by
  ``utils.trades.persist_fill`` (commit ``d079f93``). One JSON line per
  accepted fill; ``status`` is a hardcoded literal ``"filled"`` (read
  at ``utils/trades.py:92``).

Path resolution for ``trades.jsonl`` matches the writer side verbatim
(``utils/trades.py:45``): ``Path(os.environ.get("BOT_DATA_DIR", "data"))
/ "trades.jsonl"``. No coupling with ``scripts/weekly_report.py``; the
JSONL reader below is self-contained.

Locked dispositions (do not deviate without coordinator approval):

- Cold-start values: literal ``0.0`` / ``0`` when history/file lacks
  sufficient data. No schema changes; ``pnlMonth``/``pnlAllTime`` stay
  ``float`` non-nullable, ``totalTrades`` stays ``int``.
- No caching — ``trades.jsonl`` is read on demand per snapshot. If
  file growth surfaces latency at scale, a cache layer is a separate
  follow-on, NOT in this scope.
- ``pnlAllTime`` is honestly a ~6.5-month rolling window (the
  ``equity_history`` deque cap), NOT true all-time. Day-201 deque
  rollover discontinuously shifts the baseline. Baseline-persistence
  is deferred.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Iterator, Optional, Sequence

import structlog

from utils.equity_curve import compute_monthly_pnl
from utils.types import utc_now

if TYPE_CHECKING:
    from api.schemas import EquityPoint

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# PnL helpers (equity_history-derived)
# ---------------------------------------------------------------------------


def compute_pnl_month(equity_history: Sequence["EquityPoint"]) -> float:
    """Current UTC calendar month PnL.

    Reuses ``utils.equity_curve.compute_monthly_pnl`` bucketing (groups
    EquityPoints by their ``day`` field's ``YYYY-MM`` prefix; emits one
    row per month with ≥2 points). Selects the row matching the current
    UTC year-month and returns its ``pnl``.

    Returns ``0.0`` when:
      - ``equity_history`` is empty
      - The current UTC month has <2 EquityPoints (compute_monthly_pnl
        lock 6 drops single-point months)
      - The current UTC month has no rows in equity_history at all
        (e.g. cold-start within first ~24h, or all points fell off the
        deque after a long quiet period)

    Definitionally consistent with the ``monthlyPnl`` list shipped at
    ``api/routes_rest.py:340`` — same bucketing, same source, same
    ``last_equity_in_month - first_equity_in_month`` semantic.
    """
    if not equity_history:
        return 0.0
    current_month = utc_now().strftime("%Y-%m")
    rows = compute_monthly_pnl(equity_history)
    for row in rows:
        if row.month == current_month:
            return float(row.pnl)
    return 0.0


def compute_pnl_all_time(equity_history: Sequence["EquityPoint"]) -> float:
    """Cumulative PnL across the available ``equity_history`` window.

    Returns ``current_point.equity - oldest_point.equity`` over the
    sequence. Returns ``0.0`` if ``equity_history`` has <2 points.

    IMPORTANT: ``BotState.equity_history`` is a ``deque(maxlen=200)``.
    This helper computes PnL over the DEQUE WINDOW, which is at most
    ~6.5 months of daily-close history. It is NOT true all-time PnL —
    once the deque rolls over (day 201+), the oldest point drops and
    the baseline discontinuously shifts. Baseline-persistence (a
    durable startup equity that survives restarts and deque rollovers)
    is deferred to a separate scope.

    Sign: positive = gains, negative = drawdown.
    """
    if len(equity_history) < 2:
        return 0.0
    # Sequence supports __getitem__; deque does too. Index from both ends.
    oldest = equity_history[0]
    current = equity_history[-1]
    return float(current.equity) - float(oldest.equity)


# ---------------------------------------------------------------------------
# Trade count (trades.jsonl-derived)
# ---------------------------------------------------------------------------


def _resolve_trades_path() -> Path:
    """Return ``BOT_DATA_DIR/trades.jsonl``, matching the writer side
    at ``utils/trades.py:45`` verbatim. Resolved fresh on each call so
    env-var changes during a test session take effect."""
    return Path(os.environ.get("BOT_DATA_DIR", "data")) / "trades.jsonl"


def _iter_trade_records(path: Path) -> Iterator[dict]:
    """Yield one ``dict`` per non-blank, JSON-parseable line in ``path``.

    Tolerates:
      - Missing file → yields nothing (silent)
      - Blank/whitespace-only lines → skipped
      - Malformed JSON lines → skipped (logged at debug)

    Self-contained — does NOT import from ``scripts/weekly_report.py``
    (zero coupling per PR1 spec). The parsing surface is intentionally
    smaller than weekly_report's flexible-alias reader: this reader
    only needs ``status`` for trade counting.
    """
    if not path.is_file():
        return
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    yield obj
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "derived_stats_trades_read_failed",
            error=str(exc),
            path=str(path),
        )


def count_total_trades(path: Optional[Path] = None) -> int:
    """Count ``trades.jsonl`` rows whose ``status`` field equals the
    exact literal ``"filled"``.

    The literal ``"filled"`` matches the writer side at
    ``utils/trades.py:92`` (``"status": "filled",`` — hardcoded, no
    enum). Rows with any other status (``"cancelled"``, ``"canceled"``,
    ``"rejected"``, ``"pending"``, missing, etc.) are excluded.

    Returns ``0`` when the file is missing, empty, or contains no
    ``status=="filled"`` rows. Never raises.

    No caching — the file is read on demand per call. Cost is linear
    in file size. If a busy bot accumulates many MB of trades.jsonl,
    a cache layer is a separate follow-on (NOT in PR1 scope).
    """
    target = path if path is not None else _resolve_trades_path()
    count = 0
    for record in _iter_trade_records(target):
        if record.get("status") == "filled":
            count += 1
    return count
