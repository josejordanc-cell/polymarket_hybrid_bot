"""
strategies/intra_arbitrage.py
Risk-free YES+NO intra-market arbitrage.

Entry condition: YES_ask + NO_ask < 1.00 by at least min_arb_edge.
This is a guaranteed locked profit regardless of market outcome,
subject only to execution/fee risk.

Priority: 1 (highest) — executed before all other strategies.
"""
from __future__ import annotations

from typing import Any
from uuid import uuid4

import structlog

from config.settings import BotConfig
from utils.fill_correlation import record_fill_return_for_correlation
from utils.math import round_tick
from utils.types import MarketSnapshot, OrderIntent, StrategySignal, TradeResult

logger = structlog.get_logger(__name__)

# Lazy import guard for py-clob-client-v2
try:
    from py_clob_client.clob_types import OrderArgs
    _CLOB_AVAILABLE = True
except Exception:
    OrderArgs = dict  # type: ignore[assignment,misc]
    _CLOB_AVAILABLE = False


async def reconcile_arb_bundle(
    config: BotConfig,
    executor: Any,
    snapshot: MarketSnapshot,
    bundle_id: str,
    yes_intent: OrderIntent,
    no_intent: OrderIntent,
    yes_result: TradeResult,
    no_result: TradeResult,
    risk: Any,
    mm: Any,
) -> None:
    """
    After both arb IOC legs reconcile, balance exposure or unwind the overfilled side.
    Call via asyncio.create_task from the strategy loop — do not await inline.
    Exactly one bot_arb_unwinds_total outcome per bundle (balanced / unwound / skipped).
    """
    from observability.metrics_exporter import get_metrics_exporter, notify_fill_metrics

    fy = float(yes_result.filled_size)
    fn = float(no_result.filled_size)

    def _record_outcome(outcome: str) -> None:
        mx = get_metrics_exporter()
        if mx:
            mx.record_arb_unwind(outcome)

    if abs(fy - fn) < 1e-9:
        logger.info(
            "arb_bundle_balanced",
            bundle_id=bundle_id,
            filled_yes=fy,
            filled_no=fn,
        )
        _record_outcome("balanced")
        return

    imbalance = abs(fy - fn)
    max_bps = float(config.strategy.arb_max_unwind_bps)

    if fy > fn:
        over_outcome = "YES"
        entry_px = float(yes_result.fill_price or yes_intent.price_rounded())
        touch = float(snapshot.yes_bid)
        tok = yes_intent.token_id
    else:
        over_outcome = "NO"
        entry_px = float(no_result.fill_price or no_intent.price_rounded())
        touch = float(snapshot.no_bid)
        tok = no_intent.token_id

    if not tok:
        logger.warning(
            "arb_unwind_skipped",
            bundle_id=bundle_id,
            reason="missing_token_id",
            unwind_side=over_outcome,
            imbalance_size=imbalance,
        )
        _record_outcome("skipped")
        return

    expected_cost_bps = max(
        0.0, (entry_px - touch) / max(entry_px, 1e-12) * 10_000.0
    )

    if expected_cost_bps > max_bps:
        logger.warning(
            "arb_unwind_skipped",
            bundle_id=bundle_id,
            reason="unwind_cost_exceeds_max",
            unwind_side=over_outcome,
            imbalance_size=imbalance,
            expected_cost_bps=round(expected_cost_bps, 4),
            max_unwind_bps=max_bps,
        )
        _record_outcome("skipped")
        return

    tol = max_bps / 10_000.0
    raw_limit = touch * (1.0 - tol)
    limit_price = round_tick(max(0.001, min(0.999, raw_limit)), snapshot.tick_size)

    logger.info(
        "arb_unwind",
        bundle_id=bundle_id,
        imbalance_size=round(imbalance, 6),
        unwind_side=over_outcome,
        expected_cost_bps=round(expected_cost_bps, 4),
        limit_price=limit_price,
    )

    unwind = OrderIntent(
        market_id=snapshot.market_id,
        token_id=tok,
        side="SELL",
        outcome="YES" if over_outcome == "YES" else "NO",
        size=round(imbalance, 6),
        price=limit_price,
        tick_size=snapshot.tick_size,
        tif="IOC",
        strategy="intra_arbitrage",
        bundle_id=None,
    )

    tr = await executor.execute(unwind)
    if tr.accepted and tr.filled_size > 1e-12:
        _record_outcome("unwound")
        fp = tr.fill_price or unwind.price_rounded()
        risk.on_fill(unwind, fill_price=fp, filled_size=tr.filled_size)
        notify_fill_metrics(
            unwind.strategy,
            unwind.side,
            unwind.tif,
            tr.fill_price,
            tr.filled_size,
        )
        record_fill_return_for_correlation(
            risk,
            unwind,
            snapshot,
            tr.fill_price,
            tr.filled_size,
        )
        mm.on_fill(unwind, tr.filled_size)
        logger.info(
            "arb_unwind_fill_recorded",
            bundle_id=bundle_id,
            filled_size=tr.filled_size,
            fill_price=fp,
        )
    else:
        logger.warning(
            "arb_unwind_skipped",
            bundle_id=bundle_id,
            reason="unwind_order_not_filled",
            unwind_side=over_outcome,
            accepted=tr.accepted,
            filled_size=tr.filled_size,
        )
        _record_outcome("skipped")


class IntraArbitrageStrategy:
    name = "intra_arbitrage"

    def __init__(self, config: BotConfig) -> None:
        self._config = config

    # ------------------------------------------------------------------
    # Signal detection
    # ------------------------------------------------------------------

    def find_signal(self, snapshot: MarketSnapshot) -> StrategySignal | None:
        """
        Detect guaranteed-edge opportunities where YES_ask + NO_ask < 1.00.

        We require:
          - Sufficient market liquidity
          - Edge > min_arb_edge (covers exchange fees ~0.2% each side)
          - Both sides have valid token IDs (required for live execution)
        """
        rc = self._config.strategy

        if snapshot.liquidity_usd < rc.min_liquidity_usd:
            return None

        edge = 1.0 - snapshot.implied_sum
        if edge < rc.min_arb_edge:
            return None

        # Verify token IDs are available (live mode requirement)
        if self._config.runtime.mode == "live" and (
            not snapshot.yes_token_id or not snapshot.no_token_id
        ):
            logger.warning(
                "arb_skipped_missing_token_ids", market_id=snapshot.market_id
            )
            return None

        logger.info(
            "arb_signal_detected",
            market_id=snapshot.market_id,
            yes_ask=snapshot.yes_ask,
            no_ask=snapshot.no_ask,
            implied_sum=round(snapshot.implied_sum, 4),
            edge=round(edge, 4),
        )

        return StrategySignal(
            strategy_name=self.name,
            market_id=snapshot.market_id,
            side="BUY_YES",   # dummy side — we buy both
            confidence=1.0,
            expected_edge=edge,
            reference_price=snapshot.yes_ask,
            reason=f"YES+NO={snapshot.implied_sum:.4f} → locked edge={edge:.4f}",
        )

    # ------------------------------------------------------------------
    # Order building
    # ------------------------------------------------------------------

    def build_orders(
        self, snapshot: MarketSnapshot, size_usd: float
    ) -> list[OrderIntent]:
        """
        Build a hedged pair of IOC BUY orders for YES and NO.

        size_usd is the per-leg notional (each leg independently sized by
        the Kelly-capped amount from RiskManager).
        """
        if size_usd <= 0:
            return []

        bundle_id = str(uuid4())

        yes_qty = size_usd / max(snapshot.yes_ask, 0.001)
        no_qty = size_usd / max(snapshot.no_ask, 0.001)

        yes_price = round_tick(snapshot.yes_ask, snapshot.tick_size)
        no_price = round_tick(snapshot.no_ask, snapshot.tick_size)

        return [
            OrderIntent(
                market_id=snapshot.market_id,
                token_id=snapshot.yes_token_id,
                side="BUY",
                outcome="YES",
                size=round(yes_qty, 6),
                price=yes_price,
                tick_size=snapshot.tick_size,
                tif="IOC",
                strategy=self.name,
                bundle_id=bundle_id,
                signal_reference_price=snapshot.mid_yes,
            ),
            OrderIntent(
                market_id=snapshot.market_id,
                token_id=snapshot.no_token_id,
                side="BUY",
                outcome="NO",
                size=round(no_qty, 6),
                price=no_price,
                tick_size=snapshot.tick_size,
                tif="IOC",
                strategy=self.name,
                bundle_id=bundle_id,
                signal_reference_price=snapshot.mid_no,
            ),
        ]

    # ------------------------------------------------------------------
    # Live order adapter
    # ------------------------------------------------------------------

    def to_live_order_args(self, intent: OrderIntent) -> Any:
        """
        Convert an OrderIntent to py-clob-client-v2 OrderArgs.
        Caller must pass the result to client.create_and_post_order().
        """
        if not _CLOB_AVAILABLE:
            raise RuntimeError("py-clob-client-v2 is not installed")
        return OrderArgs(
            token_id=intent.token_id,
            price=intent.price_rounded(),
            size=intent.size,
            side=intent.side,
        )

    # ------------------------------------------------------------------
    # Simulation helper
    # ------------------------------------------------------------------

    async def execute_simulated(
        self, orders: list[OrderIntent]
    ) -> list[dict[str, Any]]:
        """Paper-trade the bundle; logs each fill."""
        fills = []
        for o in orders:
            fill = {
                "market_id": o.market_id,
                "outcome": o.outcome,
                "fill_price": o.price,
                "filled_size": o.size,
                "status": "sim_filled",
            }
            fills.append(fill)
        logger.info("arb_simulated", fills=len(fills))
        return fills
