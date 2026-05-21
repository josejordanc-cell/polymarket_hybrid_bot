"""
utils/risk_metrics.py
v0.2.9 B.1 wiring — pure-function helpers for RiskMetrics computation.

Closes the last B.x backlog item: the 3e canonical 6-field RiskMetrics
Pydantic locked the shape, but five of six fields (maxDrawdown, sharpe,
var95, exposurePct, liquidationRisk) shipped null because no production
source populated them. This module supplies the computation; the
snapshot helper at ``api/routes_rest.py:_build_riskmetrics`` orchestrates
the calls.

Architecture is PATH C (snapshot-build-time computation) — no background
loop, no frozen-file unfreeze, no new BotState fields. Helpers read from
the snapshot's existing surface (``equity_history``, ``open_positions``,
``equity``, ``day_start_equity``) and the live config object.

Coordinator-locked decisions referenced inline as ``(lock N)``:
  1. maxDrawdown sign: positive number (5.2 = 5.2% loss).
  2. maxDrawdown scale: percent (0–100).
  3. sharpe minimum samples: 20 daily returns.
  4. sharpe annualization: sqrt(365) — 24/7 crypto.
  5. sharpe risk-free rate: 0.
  6. var95 minimum samples: 21 daily returns.
  7. exposurePct scale: 0–100 percent.
  8. maxDrawdown semantic: all-time peak-to-trough from equity_history.
  9. var95 window: 21-day rolling (last 21 daily returns).
 10. var95 wire shape: USD (dollar loss).
 11. liquidationRisk: drawdown-proximity gradient against
     daily_drawdown_limit_pct.
"""
from __future__ import annotations

from typing import Literal, Mapping, Optional, Sequence

import numpy as np

from api.schemas import EquityPoint
from utils.types import Position


def compute_max_drawdown(
    equity_history: Sequence[EquityPoint],
) -> Optional[float]:
    """All-time peak-to-trough drawdown over ``equity_history`` (lock 8).

    Walks the deque tracking a running peak; reports the maximum
    ``(peak - trough) / peak`` observed, scaled to 0–100 percent (locks
    1 + 2). Returns ``None`` if fewer than 2 points (no drawdown
    measurable with a single point). Defensive: skips non-positive peaks
    (degenerate) and clamps the result at 0.0 (a monotone-increasing
    series has no drawdown).
    """
    if len(equity_history) < 2:
        return None

    running_peak = -1.0  # sentinel: any positive equity wins the first comparison
    max_dd_frac = 0.0
    for pt in equity_history:
        if pt.equity > running_peak:
            running_peak = pt.equity
        if running_peak > 0.0:
            dd = (running_peak - pt.equity) / running_peak
            if dd > max_dd_frac:
                max_dd_frac = dd
    return max_dd_frac * 100.0  # lock 2: percent scale


def compute_sharpe(
    equity_history: Sequence[EquityPoint],
) -> Optional[float]:
    """Annualised Sharpe ratio from daily equity returns (locks 3–5).

    Daily returns = ``(equity[i] - equity[i-1]) / equity[i-1]``.
    Sharpe = ``mean(returns) / std(returns) * sqrt(365)`` (lock 4,
    risk-free rate 0 per lock 5). Returns ``None`` when there are
    fewer than 20 daily returns (lock 3 — equivalent to ``len(history)
    < 21``) or when ``std(returns) == 0`` (degenerate zero-volatility
    case; div-by-zero guard).
    """
    if len(equity_history) < 21:  # need ≥20 returns → ≥21 points (lock 3)
        return None

    equities = np.array([pt.equity for pt in equity_history], dtype=float)
    # Daily returns: skip any pair where the prior equity is non-positive
    # to avoid div-by-zero / negative-base ratios.
    returns = np.diff(equities) / equities[:-1]
    returns = returns[np.isfinite(returns)]
    if len(returns) < 20:
        return None

    std = returns.std(ddof=0)
    if std == 0.0:
        return None  # zero volatility: Sharpe undefined
    return float(returns.mean() / std * np.sqrt(365.0))  # lock 4


def compute_var95_usd(
    equity_history: Sequence[EquityPoint],
    current_equity: float,
) -> Optional[float]:
    """21-day rolling historical 1-day VaR at 95% confidence, in USD
    (locks 6 + 9 + 10).

    Takes the last 21 daily returns; computes the 5th-percentile loss
    (a negative number); multiplies by ``-current_equity`` to convert
    to a positive dollar loss. Returns ``None`` when fewer than 21
    returns exist. Returns ``0.0`` when the 5th-percentile return is
    non-negative (no left-tail loss in the window — VaR is a
    loss metric and clamps at zero by convention).
    """
    if len(equity_history) < 22:  # need ≥21 returns → ≥22 points (lock 6)
        return None
    if current_equity <= 0.0:
        return None  # defensive: dollar conversion requires positive equity

    equities = np.array([pt.equity for pt in equity_history], dtype=float)
    returns = np.diff(equities) / equities[:-1]
    returns = returns[np.isfinite(returns)]
    if len(returns) < 21:
        return None

    window = returns[-21:]  # lock 9: 21-day rolling window
    p5 = float(np.percentile(window, 5))
    if p5 >= 0.0:
        # All-non-negative window → no left-tail loss; VaR clamps to 0.
        return 0.0
    return -p5 * current_equity  # lock 10: positive USD loss figure


def compute_exposure_pct(
    positions: Mapping[str, Position],
    equity: float,
) -> Optional[float]:
    """Open-position notional as percent of equity (lock 7).

    ``notional`` uses ``Position.notional_usd`` (``quantity * avg_price``
    per ``utils/types.py:79``) — i.e. entry-price exposure, not
    mark-to-market. Honest given that the bot's internal Position has
    no ``current_price`` field; when B.3 mark-pricing lands the numerator
    becomes more meaningful but the helper signature doesn't change.

    Returns ``None`` for non-positive equity (degenerate / fresh-start;
    div-by-zero guard).
    """
    if equity <= 0.0:
        return None
    total_notional = sum(pos.notional_usd for pos in positions.values())
    return (total_notional / equity) * 100.0  # lock 7: percent scale


def compute_liquidation_risk(
    day_start_equity: float,
    equity: float,
    daily_drawdown_limit_pct: float,
) -> Optional[Literal["low", "medium", "high"]]:
    """Repurpose ``liquidationRisk`` as a drawdown-proximity gradient
    against the configured kill-switch (lock 11).

    Polymarket spot has no liquidation surface (``leverage = 1.0``
    structural constant per 3e); this field surfaces operational
    proximity to the daily-drawdown kill-switch instead. Threshold
    table:

      dd_frac < 0.5 * limit_frac      → "low"
      0.5 * limit_frac ≤ dd_frac < 0.8 * limit_frac → "medium"
      dd_frac ≥ 0.8 * limit_frac      → "high"

    ``dd_frac`` is intra-day drawdown as a fraction (same units as
    ``daily_drawdown_limit_pct`` per ``config/settings.py:161`` —
    ``0.12 = 12%``). Comparing in fraction-space avoids unit-conversion
    arithmetic. Returns ``None`` when ``day_start_equity <= 0``
    (degenerate / fresh-start; div-by-zero guard).
    """
    if day_start_equity <= 0.0:
        return None
    dd_frac = max(0.0, (day_start_equity - equity) / day_start_equity)
    if dd_frac >= 0.8 * daily_drawdown_limit_pct:
        return "high"
    if dd_frac >= 0.5 * daily_drawdown_limit_pct:
        return "medium"
    return "low"
