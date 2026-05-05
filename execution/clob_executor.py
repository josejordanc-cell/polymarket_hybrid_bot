"""
execution/clob_executor.py
Live execution adapter for py-clob-client-v2.

py-clob-client-v2 signature notes:
  - create_and_post_order(order_args, options=None)
  - OrderArgs(token_id, price, size, side)
  - Some versions require post_only=False and neg_risk=False in options
  - L1 (EOA) wallets: no extra params needed beyond OrderArgs
  - L2 (API key) wallets: ClobClient constructed with ApiCreds

In sim mode this always delegates to PaperSimulator — no real orders.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

import structlog

from config.settings import BotConfig
from observability.metrics_exporter import get_metrics_exporter
from simulator import PaperSimulator
from utils.types import OrderIntent, TradeResult

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Lazy import with graceful fallback for environments without the SDK
# ---------------------------------------------------------------------------
try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds, OrderArgs
    from py_clob_client.order_builder.constants import Side
    _CLOB_AVAILABLE = True
except Exception:
    ClobClient = None  # type: ignore[assignment]
    _CLOB_AVAILABLE = False


@dataclass
class _PendingOrder:
    intent: OrderIntent
    submitted_size: float
    submitted_price: float
    side: str
    market_id: str
    strategy: str
    # Shared UUID for the YES+NO arb entry pair; arb unwind SELLs omit bundle_id (Prompt 3).
    bundle_id: Optional[str]
    submit_timestamp: float
    tif: str


def _parse_size_field(val: Any) -> float:
    """Parse Polymarket size (plain float or fixed-point string)."""
    if val is None:
        return 0.0
    if isinstance(val, (int, float)):
        x = float(val)
        return x / 1e6 if x >= 1_000_000 else x
    s = str(val).strip()
    if not s:
        return 0.0
    if "." in s:
        return float(s)
    iv = int(s)
    return iv / 1e6 if iv >= 1_000_000 else float(iv)


def _parse_price_field(val: Any) -> float:
    if val is None:
        return 0.0
    return float(val)


def _normalize_order_status(raw: str | None) -> str:
    s = (raw or "").upper()
    if "MATCHED" in s and "CANCEL" not in s:
        return "MATCHED"
    if "CANCEL" in s:
        return "CANCELED"
    if "LIVE" in s:
        return "LIVE"
    if "EXPIRE" in s:
        return "EXPIRED"
    return s or "UNKNOWN"


def _infer_tif(intent: OrderIntent) -> str:
    if getattr(intent, "tif", None):
        return str(intent.tif)
    st = (intent.strategy or "").lower()
    if st == "market_maker" or "market_maker" in st:
        return "GTC"
    return "IOC"


class CLOBExecutor:
    """
    Thin async wrapper around the Polymarket CLOB client.
    Always routes to PaperSimulator in sim mode — no live orders.
    """

    def __init__(
        self,
        config: BotConfig,
        *,
        fill_event_source: Optional[Callable[..., Awaitable[Any]]] = None,
    ) -> None:
        self._config = config
        self._sim = PaperSimulator(slippage_bps=config.runtime.sim_slippage_bps)
        self._client: ClobClient | None = None
        self.pending_orders: dict[str, _PendingOrder] = {}
        self._fill_event_source = fill_event_source

        if config.runtime.mode == "live":
            if not _CLOB_AVAILABLE:
                raise RuntimeError(
                    "py-clob-client-v2 is not installed.\n"
                    "Run: pip install py-clob-client"
                )
            api = config.api
            self._client = ClobClient(
                host=api.polymarket_host,
                key=api.polymarket_private_key.get_secret_value(),
                chain_id=api.chain_id,
                creds=ApiCreds(
                    api_key=api.polymarket_api_key.get_secret_value(),
                    api_secret=api.polymarket_api_secret.get_secret_value(),
                    api_passphrase=api.polymarket_api_passphrase.get_secret_value(),
                ),
            )
            logger.info("clob_executor_live", host=api.polymarket_host)

    # ------------------------------------------------------------------
    # Public execute method
    # ------------------------------------------------------------------

    async def execute(self, intent: OrderIntent) -> TradeResult:
        if self._config.runtime.mode == "sim" or self._client is None:
            return await self._sim.execute(intent)
        return await self._execute_live(intent)

    # ------------------------------------------------------------------
    # Live execution
    # ------------------------------------------------------------------

    def _get_order_snapshot(self, order_id: str) -> dict[str, Any]:
        assert self._client is not None
        raw = self._client.get_order(order_id)
        if not isinstance(raw, dict):
            return {}
        return raw

    def _log_reconciled_result(
        self,
        order_id: str,
        pending: _PendingOrder,
        tr: TradeResult,
        *,
        raw_status: str,
    ) -> None:
        logger.info(
            "order_status_changed",
            order_id=order_id,
            market=pending.market_id,
            reason=tr.reason,
            raw_status=raw_status,
            tif=pending.tif,
            filled_size=tr.filled_size,
            submitted_size=pending.submitted_size,
        )
        if tr.filled_size > 1e-12:
            logger.info(
                "fill_recorded",
                order_id=order_id,
                market=pending.market_id,
                strategy=pending.strategy,
                observed_fill_price=tr.fill_price,
                observed_filled_size=tr.filled_size,
                submitted_price=pending.submitted_price,
                submitted_size=pending.submitted_size,
                terminal_reason=tr.reason,
            )

    async def _reconcile_fills(
        self,
        order_id: str,
        pending: _PendingOrder,
    ) -> TradeResult:
        """
        Wait for terminal order state via user fill stream (if injected) or REST polling.
        """
        if self._fill_event_source is not None:
            tr = await self._fill_event_source(self, order_id, pending)
            self._log_reconciled_result(order_id, pending, tr, raw_status="injected")
            return tr

        if self._ws_user_fills_supported():
            out = await self._reconcile_via_ws(order_id, pending)
            if out is not None:
                return out

        return await self._reconcile_via_rest(order_id, pending)

    def _ws_user_fills_supported(self) -> bool:
        """py-clob-client does not ship a user/fill WebSocket helper — use REST fallback."""
        return False

    async def _reconcile_via_ws(
        self, order_id: str, pending: _PendingOrder
    ) -> TradeResult | None:
        return None

    async def _reconcile_via_rest(self, order_id: str, pending: _PendingOrder) -> TradeResult:
        interval = float(self._config.fill_poll_interval_sec)
        timeout = float(self._config.fill_poll_timeout_sec)
        deadline = time.monotonic() + timeout
        tif = pending.tif
        submitted = pending.submitted_size
        while time.monotonic() < deadline:
            snap = await asyncio.to_thread(self._get_order_snapshot, order_id)
            matched = _parse_size_field(snap.get("size_matched"))
            price = _parse_price_field(
                snap.get("avg_price")
                or snap.get("average_price")
                or snap.get("average_matched_price")
                or snap.get("price")
            )
            raw_status = str(snap.get("status") or "")
            norm = _normalize_order_status(raw_status)

            terminal, reason = self._terminal_state(
                norm, matched, submitted, tif, timed_out=False
            )
            if terminal:
                return self._finalize_terminal(
                    order_id,
                    pending,
                    matched=matched,
                    fill_price=price,
                    reason=reason,
                    raw_status=raw_status,
                )

            await asyncio.sleep(interval)

        snap = await asyncio.to_thread(self._get_order_snapshot, order_id)
        matched = _parse_size_field(snap.get("size_matched"))
        price = _parse_price_field(
            snap.get("avg_price")
            or snap.get("average_price")
            or snap.get("price")
        )
        raw_status = str(snap.get("status") or "")
        return self._finalize_terminal(
            order_id,
            pending,
            matched=matched,
            fill_price=price,
            reason="expired",
            raw_status=raw_status,
        )

    def _terminal_state(
        self,
        norm: str,
        matched: float,
        submitted: float,
        tif: str,
        *,
        timed_out: bool,
    ) -> tuple[bool, str | None]:
        if timed_out:
            return True, "expired"
        eps = 1e-9
        if matched + eps >= submitted:
            return True, "matched"
        if norm == "CANCELED":
            if matched <= eps:
                return True, "canceled_no_fill"
            if tif == "IOC" and matched + eps < submitted:
                return True, "ioc_partial_cancelled"
            return True, "canceled"
        if norm == "MATCHED":
            return True, "matched"
        if norm == "EXPIRED":
            return True, "expired"
        return False, None

    def _finalize_terminal(
        self,
        order_id: str,
        pending: _PendingOrder,
        *,
        matched: float,
        fill_price: float,
        reason: str | None,
        raw_status: str,
    ) -> TradeResult:
        tr = TradeResult(
            accepted=True,
            order_id=order_id,
            fill_price=fill_price if matched > 1e-12 else None,
            filled_size=matched,
            reason=reason,
        )
        self._log_reconciled_result(order_id, pending, tr, raw_status=raw_status)
        return tr

    async def _execute_live(self, intent: OrderIntent) -> TradeResult:
        """
        Submit a single order via py-clob-client-v2, then reconcile real fills
        (REST polling fallback unless a user fill WebSocket is available).
        """
        if not _CLOB_AVAILABLE or self._client is None:
            return TradeResult(accepted=False, reason="clob_client_unavailable")

        tif = _infer_tif(intent)

        try:
            order_args = OrderArgs(
                token_id=intent.token_id,
                price=intent.price_rounded(),
                size=intent.size,
                side=Side.BUY if intent.side == "BUY" else Side.SELL,
            )

            resp = self._client.create_and_post_order(order_args)

            order_id = str(resp.get("orderID") or resp.get("id") or "")
            status = str(resp.get("status") or "")
            error_msg = str(resp.get("errorMsg") or "")

            accepted = status not in {"unmatched_error", "error", "cancelled"} and not error_msg

            if not accepted:
                logger.warning(
                    "live_order_rejected",
                    market=intent.market_id,
                    status=status,
                    error=error_msg,
                )
                mx = get_metrics_exporter()
                if mx:
                    reason = error_msg or status or "rejected"
                    mx.record_rejection(reason)
                return TradeResult(
                    accepted=False,
                    order_id=order_id if order_id else None,
                    reason=error_msg or status,
                )

            if not order_id:
                logger.error("live_order_missing_order_id", market=intent.market_id, resp=resp)
                mx = get_metrics_exporter()
                if mx:
                    mx.record_rejection("missing_order_id")
                return TradeResult(accepted=False, reason="missing_order_id")

            logger.info(
                "live_order_accepted",
                market=intent.market_id,
                outcome=intent.outcome,
                side=intent.side,
                price=intent.price_rounded(),
                size=intent.size,
                order_id=order_id,
            )

            pending = _PendingOrder(
                intent=intent,
                submitted_size=float(intent.size),
                submitted_price=float(intent.price_rounded()),
                side=intent.side,
                market_id=intent.market_id,
                strategy=intent.strategy,
                bundle_id=getattr(intent, "bundle_id", None),
                submit_timestamp=time.time(),
                tif=tif,
            )
            self.pending_orders[order_id] = pending
            try:
                result = await self._reconcile_fills(order_id, pending)
            finally:
                self.pending_orders.pop(order_id, None)

            return result

        except Exception as exc:
            logger.exception(
                "live_order_exception",
                market=intent.market_id,
                error=str(exc),
            )
            mx = get_metrics_exporter()
            if mx:
                mx.record_rejection(f"exception:{exc!s}"[:200])
            return TradeResult(accepted=False, reason=str(exc))
