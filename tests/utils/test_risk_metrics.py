"""tests/utils/test_risk_metrics.py — v0.2.9 B.1 wiring tests.

Covers utils/risk_metrics.py — the five pure-function helpers that
populate RiskMetrics from BotState surface (equity_history, positions,
equity, day_start_equity) + RiskConfig.daily_drawdown_limit_pct.

All 11 coordinator-locked decisions are exercised here; the test class
docstrings reference the lock numbers.
"""
from __future__ import annotations

from collections import deque

import numpy as np
import pytest

from api.schemas import EquityPoint
from utils.risk_metrics import (
    compute_exposure_pct,
    compute_liquidation_risk,
    compute_max_drawdown,
    compute_sharpe,
    compute_sortino,
    compute_var95_usd,
)
from utils.types import Position


def _ep(day: str, equity: float, pnl: float = 0.0) -> EquityPoint:
    """Compact EquityPoint constructor for tests."""
    return EquityPoint(day=day, equity=equity, pnl=pnl)


# ---------------------------------------------------------------------------
# TestComputeMaxDrawdown — lock 1 (positive sign), lock 2 (percent),
# lock 8 (all-time peak-to-trough from equity_history)
# ---------------------------------------------------------------------------


class TestComputeMaxDrawdown:
    def test_typical_peak_to_trough(self) -> None:
        """10000 → 9000 → 10500: peak=10500 ... wait, peak=10000 first,
        trough=9000 → 10% drawdown. Then 10500 > 10000 is a new peak.
        The 10% drawdown is the max observed. Returns positive 10.0."""
        history = [
            _ep("2026-05-19", 10000.0),
            _ep("2026-05-20", 9000.0),
            _ep("2026-05-21", 10500.0),
        ]
        assert compute_max_drawdown(history) == pytest.approx(10.0)

    def test_monotone_increasing_zero_drawdown(self) -> None:
        history = [
            _ep("2026-05-19", 10000.0),
            _ep("2026-05-20", 10500.0),
            _ep("2026-05-21", 11000.0),
        ]
        assert compute_max_drawdown(history) == pytest.approx(0.0)

    def test_single_point_returns_none(self) -> None:
        history = [_ep("2026-05-21", 10000.0)]
        assert compute_max_drawdown(history) is None

    def test_empty_returns_none(self) -> None:
        assert compute_max_drawdown([]) is None

    def test_deep_drawdown(self) -> None:
        """Peak 10000 → trough 5000 = 50% drawdown (positive per lock 1)."""
        history = [_ep("2026-05-19", 10000.0), _ep("2026-05-20", 5000.0)]
        assert compute_max_drawdown(history) == pytest.approx(50.0)


# ---------------------------------------------------------------------------
# TestComputeSharpe — lock 3 (≥20 returns), lock 4 (sqrt(365)),
# lock 5 (rf=0)
# ---------------------------------------------------------------------------


class TestComputeSharpe:
    def _make_history(self, returns: list[float], start: float = 10000.0) -> list[EquityPoint]:
        """Build an equity_history from a sequence of returns."""
        equities = [start]
        for r in returns:
            equities.append(equities[-1] * (1.0 + r))
        return [_ep(f"2026-05-{i+1:02d}", e) for i, e in enumerate(equities)]

    def test_positive_returns_happy_path(self) -> None:
        """20 consistent 1% gains → positive finite Sharpe."""
        history = self._make_history([0.01] * 20)
        sharpe = compute_sharpe(history)
        assert sharpe is not None
        assert sharpe > 0
        assert np.isfinite(sharpe)

    def test_mixed_returns_annualization_factor(self) -> None:
        """Manually compute Sharpe for a known sequence and verify
        annualization with sqrt(365) (lock 4)."""
        rets = [0.01, -0.005, 0.02, -0.01, 0.015, -0.008, 0.012] * 3  # 21 returns
        history = self._make_history(rets[:20])
        sharpe = compute_sharpe(history)
        assert sharpe is not None
        # Recompute by hand to verify the factor.
        equities = np.array([pt.equity for pt in history], dtype=float)
        manual_rets = np.diff(equities) / equities[:-1]
        expected = float(manual_rets.mean() / manual_rets.std(ddof=0) * np.sqrt(365.0))
        assert sharpe == pytest.approx(expected)

    def test_all_zero_returns_returns_none(self) -> None:
        """std == 0 (degenerate zero-volatility) → None to avoid div-by-zero."""
        history = self._make_history([0.0] * 20)
        assert compute_sharpe(history) is None

    def test_19_returns_insufficient(self) -> None:
        """Need ≥20 daily returns per lock 3; 19 returns (20 points) → None."""
        history = self._make_history([0.01] * 19)
        assert compute_sharpe(history) is None

    def test_empty_returns_none(self) -> None:
        assert compute_sharpe([]) is None


# ---------------------------------------------------------------------------
# TestComputeSortino — mirrors TestComputeSharpe locks 3–5
# (≥20 returns, sqrt(365) annualization, MAR=rf=0). Downside-deviation
# denominator is std of NEGATIVE returns only.
# ---------------------------------------------------------------------------


class TestComputeSortino:
    def _make_history(self, returns: list[float], start: float = 10000.0) -> list[EquityPoint]:
        """Build an equity_history from a sequence of returns."""
        equities = [start]
        for r in returns:
            equities.append(equities[-1] * (1.0 + r))
        return [_ep(f"2026-05-{i+1:02d}", e) for i, e in enumerate(equities)]

    def test_mixed_returns_happy_path(self) -> None:
        """20 returns with a mix of pos/neg → finite Sortino."""
        rets = [0.01, -0.005, 0.02, -0.01, 0.015, -0.008, 0.012] * 3  # 21 returns
        history = self._make_history(rets[:20])
        sortino = compute_sortino(history)
        assert sortino is not None
        assert np.isfinite(sortino)

    def test_mixed_returns_annualization_factor(self) -> None:
        """Manually compute Sortino for a known sequence and verify
        annualization with sqrt(365) (lock 4) and MAR=0 (lock 5)."""
        rets = [0.01, -0.005, 0.02, -0.01, 0.015, -0.008, 0.012] * 3  # 21 returns
        history = self._make_history(rets[:20])
        sortino = compute_sortino(history)
        assert sortino is not None
        equities = np.array([pt.equity for pt in history], dtype=float)
        manual_rets = np.diff(equities) / equities[:-1]
        downside = manual_rets[manual_rets < 0.0]
        expected = float(manual_rets.mean() / downside.std(ddof=0) * np.sqrt(365.0))
        assert sortino == pytest.approx(expected)

    def test_all_positive_returns_zero_downside_returns_none(self) -> None:
        """No downside returns → Sortino denominator undefined → None
        (per spec: do not divide by zero, do not return inf)."""
        history = self._make_history([0.01] * 20)
        assert compute_sortino(history) is None

    def test_19_returns_insufficient(self) -> None:
        """Need ≥20 daily returns per lock 3; 19 returns → None."""
        history = self._make_history([0.01, -0.005] * 10)  # 20 returns
        # Drop one to make it 19 returns (20 points).
        assert compute_sortino(history[:-1]) is None

    def test_empty_returns_none(self) -> None:
        assert compute_sortino([]) is None


# ---------------------------------------------------------------------------
# TestComputeVar95Usd — lock 6 (≥21 returns), lock 9 (21-day window),
# lock 10 (USD wire shape)
# ---------------------------------------------------------------------------


class TestComputeVar95Usd:
    def _make_history(self, returns: list[float], start: float = 10000.0) -> list[EquityPoint]:
        equities = [start]
        for r in returns:
            equities.append(equities[-1] * (1.0 + r))
        return [_ep(f"2026-05-{i+1:02d}", e) for i, e in enumerate(equities)]

    def test_typical_losses_positive_usd(self) -> None:
        """21 returns including some 5%+ losses → positive USD VaR."""
        # Mix of small gains and some sharper losses.
        rets = [0.005, -0.04, 0.01, -0.06, 0.003, -0.08, 0.002] * 3  # 21 returns
        history = self._make_history(rets)
        current_equity = history[-1].equity
        var = compute_var95_usd(history, current_equity)
        assert var is not None
        assert var > 0  # left-tail loss exists
        # Sanity: var should be a small fraction of equity (typical VaR scale).
        assert var < current_equity

    def test_20_returns_insufficient(self) -> None:
        """Need ≥21 daily returns per lock 6 (i.e. ≥22 equity points)."""
        history = self._make_history([0.005] * 20)
        assert compute_var95_usd(history, current_equity=10000.0) is None

    def test_all_non_negative_returns_clamps_to_zero(self) -> None:
        """No left-tail loss in the window → VaR clamps to 0.0 (not None,
        not negative). Spec note in lock 10 + helper docstring."""
        history = self._make_history([0.01] * 21)
        var = compute_var95_usd(history, current_equity=10000.0)
        assert var == 0.0

    def test_empty_returns_none(self) -> None:
        assert compute_var95_usd([], current_equity=10000.0) is None

    def test_non_positive_equity_returns_none(self) -> None:
        """Dollar conversion is undefined for non-positive equity."""
        history = self._make_history([-0.01] * 21)
        assert compute_var95_usd(history, current_equity=0.0) is None
        assert compute_var95_usd(history, current_equity=-100.0) is None


# ---------------------------------------------------------------------------
# TestComputeExposurePct — lock 7 (0–100 percent scale)
# ---------------------------------------------------------------------------


def _make_position(
    market_id: str = "0xabc",
    outcome: str = "YES",
    quantity: float = 100.0,
    avg_price: float = 0.55,
    strategy: str = "intra_arbitrage",
) -> Position:
    return Position(
        market_id=market_id,
        outcome=outcome,
        quantity=quantity,
        avg_price=avg_price,
        strategy=strategy,
    )


class TestComputeExposurePct:
    def test_empty_positions_returns_zero(self) -> None:
        assert compute_exposure_pct({}, equity=10000.0) == 0.0

    def test_multi_position_sum_over_equity(self) -> None:
        """Two positions: 100 * 0.55 + 50 * 0.40 = 55 + 20 = 75 notional.
        At equity 1500 → 5.0 percent."""
        positions = {
            "k1": _make_position(quantity=100.0, avg_price=0.55),
            "k2": _make_position(market_id="0xdef", quantity=50.0, avg_price=0.40),
        }
        assert compute_exposure_pct(positions, equity=1500.0) == pytest.approx(5.0)

    def test_zero_equity_returns_none(self) -> None:
        """Degenerate guard: div-by-zero protection."""
        positions = {"k1": _make_position()}
        assert compute_exposure_pct(positions, equity=0.0) is None

    def test_negative_equity_returns_none(self) -> None:
        positions = {"k1": _make_position()}
        assert compute_exposure_pct(positions, equity=-100.0) is None


# ---------------------------------------------------------------------------
# TestComputeLiquidationRisk — lock 11 (drawdown-proximity gradient)
# ---------------------------------------------------------------------------


class TestComputeLiquidationRisk:
    """Threshold table (lock 11, comparing in fraction-space):
        dd_frac < 0.5 * limit                → "low"
        0.5 * limit ≤ dd_frac < 0.8 * limit  → "medium"
        dd_frac ≥ 0.8 * limit                → "high"

    With daily_drawdown_limit_pct=0.12 (default 12%):
        boundaries: 6% and 9.6%.
    """

    LIMIT = 0.12  # default from config/settings.py:161

    def test_zero_drawdown_low(self) -> None:
        """equity == day_start_equity → dd=0 → 'low'."""
        risk = compute_liquidation_risk(
            day_start_equity=10000.0,
            equity=10000.0,
            daily_drawdown_limit_pct=self.LIMIT,
        )
        assert risk == "low"

    def test_30pct_of_limit_low(self) -> None:
        """dd_frac = 0.036 = 30% of limit (0.12 * 0.3) → 'low'."""
        # 10000 → 9640 = 3.6% drawdown = 30% of the 12% limit.
        risk = compute_liquidation_risk(
            day_start_equity=10000.0,
            equity=9640.0,
            daily_drawdown_limit_pct=self.LIMIT,
        )
        assert risk == "low"

    def test_60pct_of_limit_medium(self) -> None:
        """dd_frac = 0.072 = 60% of limit → 'medium' (≥ 50% boundary)."""
        risk = compute_liquidation_risk(
            day_start_equity=10000.0,
            equity=9280.0,
            daily_drawdown_limit_pct=self.LIMIT,
        )
        assert risk == "medium"

    def test_90pct_of_limit_high(self) -> None:
        """dd_frac = 0.108 = 90% of limit → 'high' (≥ 80% boundary)."""
        risk = compute_liquidation_risk(
            day_start_equity=10000.0,
            equity=8920.0,
            daily_drawdown_limit_pct=self.LIMIT,
        )
        assert risk == "high"

    def test_zero_day_start_equity_returns_none(self) -> None:
        """Defensive guard: degenerate / fresh-start state."""
        risk = compute_liquidation_risk(
            day_start_equity=0.0,
            equity=0.0,
            daily_drawdown_limit_pct=self.LIMIT,
        )
        assert risk is None

    def test_negative_day_start_equity_returns_none(self) -> None:
        risk = compute_liquidation_risk(
            day_start_equity=-100.0,
            equity=0.0,
            daily_drawdown_limit_pct=self.LIMIT,
        )
        assert risk is None

    def test_equity_above_day_start_floors_to_low(self) -> None:
        """Profitable day: equity > day_start_equity → dd clamps to 0
        → 'low' (not None, not negative)."""
        risk = compute_liquidation_risk(
            day_start_equity=10000.0,
            equity=10500.0,
            daily_drawdown_limit_pct=self.LIMIT,
        )
        assert risk == "low"

    def test_boundary_exactly_50pct_is_medium(self) -> None:
        """≥ 50% boundary is INCLUSIVE per lock 11 (`≥ 0.5 * limit`)."""
        # dd_frac = 0.06 exactly = 50% of 0.12.
        risk = compute_liquidation_risk(
            day_start_equity=10000.0,
            equity=9400.0,
            daily_drawdown_limit_pct=self.LIMIT,
        )
        assert risk == "medium"
