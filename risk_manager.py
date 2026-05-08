"""
risk_manager.py
Kelly sizing (fee-adjusted), daily drawdown kill-switch,
per-trade caps, open-position limits, and correlation guards.

All thresholds are NON-NEGOTIABLE: they are enforced here and
cannot be overridden by individual strategies.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import date

import numpy as np
import structlog

from config.settings import BotConfig
from utils.types import DailyPnL, OrderIntent, Position, StrategySignal

logger = structlog.get_logger(__name__)

# Polymarket taker fee per side. Adjust if you receive maker rebates.
POLYMARKET_FEE_PER_SIDE = 0.002   # 0.20 %
ROUND_TRIP_FEE = POLYMARKET_FEE_PER_SIDE * 2   # 0.40 %


@dataclass(slots=True)
class _RiskState:
    equity: float
    day_start_equity: float
    today: date
    realised_pnl: float = 0.0
    open_positions: dict[str, Position] = field(default_factory=dict)
    trade_count_today: int = 0


class RiskManager:
    """
    Central risk authority.

    Usage pattern:
        approved = risk.approve_orders(intents)
        for intent in approved:
            result = await executor.execute(intent)
            risk.on_fill(intent, result.fill_price, result.filled_size)
    """

    def __init__(self, config: BotConfig) -> None:
        self._cfg = config
        bankroll = config.risk.bankroll_usd
        self._state = _RiskState(
            equity=bankroll,
            day_start_equity=bankroll,
            today=date.today(),
        )
        self._return_history: dict[str, deque[float]] = defaultdict(
            lambda: deque(maxlen=120)
        )

    def _alert(self, msg: str, level: str = "info") -> None:
        """Non-blocking fire-and-forget alert."""
        try:
            from utils.alerts import send_alert
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(send_alert(msg, level=level))  # type: ignore[arg-type]
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Public properties
    # ------------------------------------------------------------------

    @property
    def equity(self) -> float:
        return self._state.equity

    @property
    def open_positions(self) -> dict[str, Position]:
        return dict(self._state.open_positions)

    def daily_pnl_snapshot(self) -> DailyPnL:
        s = self._state
        return DailyPnL(
            date_str=s.today.isoformat(),
            start_equity_usd=s.day_start_equity,
            realised_pnl_usd=s.realised_pnl,
            num_trades=s.trade_count_today,
        )

    # ------------------------------------------------------------------
    # Kill-switch
    # ------------------------------------------------------------------

    def _maybe_reset_day(self) -> None:
        today = date.today()
        if self._state.today != today:
            logger.info("new_trading_day", prev_pnl=self._state.realised_pnl)
            self._state.today = today
            self._state.day_start_equity = self._state.equity
            self._state.realised_pnl = 0.0
            self._state.trade_count_today = 0

    def kill_switch_active(self) -> bool:
        """Return True when daily drawdown limit has been breached."""
        self._maybe_reset_day()
        s = self._state
        loss = s.day_start_equity - s.equity
        dd_pct = loss / max(s.day_start_equity, 1.0)
        if dd_pct >= self._cfg.risk.daily_drawdown_limit_pct:
            logger.error(
                "kill_switch_triggered",
                drawdown_pct=round(dd_pct, 4),
                limit=self._cfg.risk.daily_drawdown_limit_pct,
                equity=s.equity,
            )
            from observability.metrics_exporter import get_metrics_exporter

            mx = get_metrics_exporter()
            if mx:
                mx.set_kill_switch(True)

            self._alert(
                f"🚨 Kill-switch! Drawdown={dd_pct:.1%} equity=${s.equity:,.2f}",
                level="critical",
            )
            return True
        return False

    # ------------------------------------------------------------------
    # Fee-aware Kelly sizing
    # ------------------------------------------------------------------

    def kelly_size_usd(self, signal: StrategySignal) -> float:
        """
        Quarter-Kelly (configurable fraction) sizing, adjusted for fees.

        Net edge = gross expected_edge − round_trip_fee (0.4 %)
        Negative net edge → size = 0 (trade rejected before approve_orders).

        Kelly formula:  f* = (b·p − q) / b
            b  = decimal odds = (1 − price) / price
            p  = P(win) estimated conservatively from net edge
            q  = 1 − p
        """
        net_edge = signal.expected_edge - ROUND_TRIP_FEE
        if net_edge <= 0:
            logger.debug(
                "kelly_zero_negative_net_edge",
                market=signal.market_id,
                gross=signal.expected_edge,
                fee=ROUND_TRIP_FEE,
            )
            return 0.0

        p = min(0.90, max(0.51, 0.50 + net_edge * 0.5))
        q = 1.0 - p
        b = (1.0 - signal.reference_price) / max(signal.reference_price, 0.01)
        b = max(b, 0.05)

        raw_kelly = max(0.0, (b * p - q) / b)
        fractional = self._cfg.risk.kelly_fraction * raw_kelly
        capped = min(fractional, self._cfg.risk.max_trade_pct)
        notional = self._state.equity * capped

        if notional < self._cfg.risk.min_order_usd:
            return 0.0

        logger.debug(
            "kelly_size",
            market=signal.market_id,
            gross_edge=round(signal.expected_edge, 4),
            net_edge=round(net_edge, 4),
            p=round(p, 3),
            b=round(b, 3),
            raw_kelly=round(raw_kelly, 4),
            notional_usd=round(notional, 2),
        )
        return notional

    # ------------------------------------------------------------------
    # Correlation guard
    # ------------------------------------------------------------------

    def record_return(self, market_id: str, ret: float) -> None:
        self._return_history[market_id].append(ret)

    def correlation_blocked(self, market_id: str) -> bool:
        hist_new = np.asarray(self._return_history[market_id])
        if len(hist_new) < 20:
            return False
        open_market_ids = {
            pos.market_id
            for pos in self._state.open_positions.values()
            if pos.market_id != market_id
        }
        for open_id in open_market_ids:
            hist_open = np.asarray(self._return_history.get(open_id, []))
            n = min(len(hist_new), len(hist_open))
            if n < 20:
                continue
            corr = float(np.corrcoef(hist_new[-n:], hist_open[-n:])[0, 1])
            if abs(corr) > self._cfg.risk.max_pair_correlation:
                logger.warning(
                    "correlation_block",
                    market=market_id,
                    correlated_with=open_id,
                    corr=round(corr, 3),
                )
                return True
        return False

    # ------------------------------------------------------------------
    # Order approval
    # ------------------------------------------------------------------

    def approve_orders(self, intents: list[OrderIntent]) -> list[OrderIntent]:
        if self.kill_switch_active():
            return []

        s = self._state
        rc = self._cfg.risk
        approved: list[OrderIntent] = []

        for intent in intents:
            if len(s.open_positions) >= rc.max_open_positions:
                logger.warning("position_cap_reached", limit=rc.max_open_positions)
                break

            if intent.strategy != "intra_arbitrage" and self.correlation_blocked(
                intent.market_id
            ):
                continue

            max_notional = s.equity * rc.max_trade_pct
            if intent.notional_usd > max_notional:
                scale = max_notional / max(intent.notional_usd, 1e-9)
                intent = intent.model_copy(
                    update={"size": round(intent.size * scale, 6)}
                )

            if intent.notional_usd < rc.min_order_usd:
                continue

            approved.append(intent)

        return approved

    # ------------------------------------------------------------------
    # Fill accounting
    # ------------------------------------------------------------------

    def on_fill(self, intent: OrderIntent, fill_price: float, filled_size: float) -> None:
        s = self._state
        cost = fill_price * filled_size
        fee = cost * POLYMARKET_FEE_PER_SIDE

        key = f"{intent.market_id}_{intent.outcome}"

        if intent.side == "BUY":
            s.equity -= cost + fee
            if key in s.open_positions:
                pos = s.open_positions[key]
                total_qty = pos.quantity + filled_size
                avg = (pos.avg_price * pos.quantity + fill_price * filled_size) / total_qty
                s.open_positions[key] = pos.model_copy(
                    update={"quantity": total_qty, "avg_price": avg}
                )
            else:
                s.open_positions[key] = Position(
                    market_id=intent.market_id,
                    outcome=intent.outcome,
                    quantity=filled_size,
                    avg_price=fill_price,
                    strategy=intent.strategy,
                )
        else:
            s.equity += cost - fee
            if key in s.open_positions:
                pos = s.open_positions[key]
                new_q = pos.quantity - filled_size
                if new_q <= 1e-9:
                    del s.open_positions[key]
                else:
                    s.open_positions[key] = pos.model_copy(update={"quantity": new_q})
            else:
                logger.warning(
                    "sell_fill_without_open_position",
                    market=intent.market_id,
                    outcome=intent.outcome,
                    size=filled_size,
                )

        s.trade_count_today += 1
        logger.info(
            "fill_recorded",
            market=intent.market_id,
            outcome=intent.outcome,
            side=intent.side,
            fill_price=fill_price,
            size=filled_size,
            fee_usd=round(fee, 4),
            equity=round(s.equity, 2),
        )

        from observability.metrics_exporter import get_metrics_exporter

        mx = get_metrics_exporter()
        if mx:
            mx.update_equity(s.equity)
            mx.update_open_positions(len(s.open_positions))

        if cost >= self._cfg.risk.bankroll_usd * 0.05:
            self._alert(
                f"ℹ️ Big fill: {intent.strategy} {intent.outcome} "
                f"${cost:,.2f} @ {fill_price:.3f} | equity=${s.equity:,.2f}",
                level="info",
            )

    def on_settlement(self, market_id: str, winning_outcome: str) -> None:
        s = self._state
        for outcome in ("YES", "NO"):
            key = f"{market_id}_{outcome}"
            if key not in s.open_positions:
                continue
            pos = s.open_positions.pop(key)
            proceeds = pos.quantity * 1.0 if outcome == winning_outcome else 0.0
            pnl = proceeds - pos.notional_usd
            s.equity += proceeds
            s.realised_pnl += pnl
            logger.info(
                "settlement",
                market=market_id,
                outcome=outcome,
                winning=outcome == winning_outcome,
                pnl=round(pnl, 4),
                equity=round(s.equity, 2),
            )
            from observability.metrics_exporter import get_metrics_exporter

            mx = get_metrics_exporter()
            if mx:
                mx.update_equity(s.equity)
                mx.update_open_positions(len(s.open_positions))

            emoji = "✅" if pnl >= 0 else "❌"
            self._alert(
                f"{emoji} Settlement {market_id[:12]}… {outcome} "
                f"PnL=${pnl:+.2f} equity=${s.equity:,.2f}",
                level="info" if pnl >= 0 else "warning",
            )

