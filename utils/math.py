"""
utils/math.py
Shared math utilities used across strategies and risk management.
Centralised here to avoid copy-paste drift across strategy files.
"""
from __future__ import annotations

import numpy as np


def round_tick(price: float, tick_size: float) -> float:
    """
    Snap a price to the nearest valid tick size.
    Required by py-clob-client-v2 before order submission.

    Example:
        round_tick(0.4732, 0.01)  → 0.47
        round_tick(0.4750, 0.01)  → 0.48  (rounds half-up via round())
    """
    if tick_size <= 0:
        return price
    return round(round(price / tick_size) * tick_size, 6)


def kelly_fraction(
    expected_edge: float,
    reference_price: float,
    round_trip_fee: float = 0.004,
    kelly_multiplier: float = 0.25,
    max_fraction: float = 0.10,
) -> float:
    """
    Compute fractional Kelly bet size as a fraction of bankroll.

    Args:
        expected_edge:    Gross edge estimate (0–1).
        reference_price:  Entry price (0–1), used to derive odds b.
        round_trip_fee:   Total fee cost (default 0.4% = 0.004).
        kelly_multiplier: Fraction of full Kelly to use (default quarter-Kelly).
        max_fraction:     Hard cap on fraction (default 10%).

    Returns:
        Fraction of bankroll to wager (0.0 if edge doesn't clear fees).
    """
    net_edge = expected_edge - round_trip_fee
    if net_edge <= 0:
        return 0.0

    p = float(np.clip(0.50 + net_edge * 0.5, 0.51, 0.90))
    q = 1.0 - p
    b = (1.0 - reference_price) / max(reference_price, 0.01)
    b = max(b, 0.05)

    raw = max(0.0, (b * p - q) / b)
    return float(np.clip(kelly_multiplier * raw, 0.0, max_fraction))


def compute_z_score(series: np.ndarray) -> float | None:
    """
    Compute z-score of the last element vs the rest of the series.
    Returns None if series is too short or has zero variance.
    """
    if len(series) < 10:
        return None
    std = float(np.std(series))
    if std < 1e-10:
        return None
    return float((series[-1] - np.mean(series)) / std)


def log_returns(prices: np.ndarray) -> np.ndarray:
    """
    Compute log-return series from a price array.
    More stationary than simple returns for z-score computation.
    """
    clipped = np.clip(prices, 1e-9, None)
    return np.diff(np.log(clipped))

