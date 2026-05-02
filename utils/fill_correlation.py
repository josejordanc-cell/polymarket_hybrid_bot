"""
Per-fill return recording for RiskManager correlation guard (Prompt 5).

Keeps import graph acyclic: strategies and main call into here instead of
circular main ↔ intra_arbitrage imports.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from utils.types import MarketSnapshot, OrderIntent

if TYPE_CHECKING:
    from risk_manager import RiskManager


def record_fill_return_for_correlation(
    risk: RiskManager,
    intent: OrderIntent,
    snapshot: MarketSnapshot,
    fill_price: float | None,
    filled_size: float,
) -> None:
    """Compute per-trade return vs reference and feed RiskManager.record_return."""
    if filled_size <= 0:
        return

    eff_price = float(fill_price) if fill_price is not None else intent.price_rounded()
    ref = intent.signal_reference_price
    if ref is None or ref <= 0:
        ref = snapshot.mid_yes if intent.outcome == "YES" else snapshot.mid_no

    direction = 1.0 if intent.side == "BUY" else -1.0
    denom = max(ref, 1e-12)
    return_value = (eff_price - ref) / denom * direction
    risk.record_return(intent.market_id, return_value)
