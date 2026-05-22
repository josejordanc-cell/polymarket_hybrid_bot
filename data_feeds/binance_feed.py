"""
data_feeds/binance_feed.py
Real-time spot price feed for BTC, ETH, SOL via ccxt.pro WebSocket.

Maintains a rolling return history per symbol for momentum/z-score strategies.
"""
from __future__ import annotations

import asyncio
import os
import time
from collections import defaultdict, deque
from datetime import datetime

import numpy as np
import structlog

from api.schemas import PriceTick
from config.settings import BotConfig
from utils.fills import get_emitter
from utils.types import SpotTick

logger = structlog.get_logger(__name__)

_MAX_BACKOFF = 30.0
# Number of ticks retained per symbol for z-score computation
_HISTORY_LEN = 500
# v0.2.9 3i policy lock 3: TICK WS emit is throttled per (venue, symbol)
# stream at 250 ms — max 4 frames/sec/stream, ~24/sec aggregate at peak
# across the configured 3 symbols × 2 venues = 6 streams.
_TICK_EMIT_INTERVAL_SEC = 0.25


def _build_price_tick(symbol: str, price: float, ts: datetime) -> PriceTick:
    """Build a wire ``PriceTick`` from spot-feed inputs.

    v0.2.9 3i policy locks applied:
      - Lock 1: ``symbol`` is the venue's native BASE/QUOTE form
        verbatim (no normalization).
      - Lock 4: ``bid`` / ``ask`` ship ``None``. **Deferred
        indefinitely** per Round 8's ``v029-bidask-close`` decision:
        zero dashboard consumers today; reopen when a spread /
        orderbook panel is added. The feed discards ccxt's
        ``ticker["bid"]`` / ``["ask"]`` at ingestion (line ~97);
        adding extraction is a small change (~10 LOC) but
        unmotivated without a consumer.
      - Lock 5: ``ts`` is ``int(ts.timestamp() * 1000)`` — bot's own
        ``utc_now()`` converted to 13-digit epoch ms (matches Team A's
        3g ``.refine(v >= 1e12 && v < 1e14)`` guard).
      - Lock 6: field names match Zod
        (``components/dashboard/schemas.ts:211``) byte-for-byte.
    """
    return PriceTick(
        symbol=symbol,
        last=price,
        ts=int(ts.timestamp() * 1000),
        # bid/ask intentionally omitted — Pydantic defaults to None.
        # Deferred indefinitely per v029-bidask-close (Round 8);
        # reopen when a spread/orderbook panel is added.
    )


class BinanceFeed:
    """
    Subscribes to Binance (primary) and Coinbase (secondary) via ccxt.pro.
    Falls back gracefully if a venue is unavailable.
    Exposes:
        - get_mid_price(symbol)  — average across available venues
        - get_z_score(symbol, window)  — momentum z-score
        - get_return_series(symbol)    — raw return array for correlation
    """

    def __init__(self, config: BotConfig) -> None:
        self._config = config
        # price_history[venue][symbol] = deque of prices
        self._price_history: dict[str, dict[str, deque[float]]] = defaultdict(
            lambda: defaultdict(lambda: deque(maxlen=_HISTORY_LEN))
        )
        self._last_ticks: dict[str, dict[str, SpotTick]] = defaultdict(dict)
        self._ticks_seen: dict[str, int] = {}
        self._last_tick_at: dict[str, float] = {}
        self._lock = asyncio.Lock()
        # v0.2.9 3i: per-(venue, symbol) throttle state for TICK WS emit.
        # time.monotonic() values — immune to wall-clock jumps.
        self._last_emit: dict[tuple[str, str], float] = {}
        # Tracks streams that have already logged "tick_emit_started" so
        # the INFO log fires exactly once per stream per bot startup.
        self._emit_logged: set[tuple[str, str]] = set()

    # ------------------------------------------------------------------
    # Run loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        tasks = [
            asyncio.create_task(self._run_venue("binance"), name="binance_spot"),
            asyncio.create_task(self._run_venue("coinbase"), name="coinbase_spot"),
            asyncio.create_task(self._heartbeat_loop(), name="spot_feed_heartbeat"),
        ]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _run_venue(self, venue: str) -> None:
        """Subscribe to all configured symbols for a given venue."""
        if venue == "binance" and os.getenv(
            "BOT_DISABLE_BINANCE_FEED", ""
        ).lower() in ("1", "true", "yes"):
            logger.info("binance_feed_disabled", reason="BOT_DISABLE_BINANCE_FEED=true")
            return

        cfg = self._config.api
        symbols = (
            cfg.binance_symbols if venue == "binance" else cfg.coinbase_symbols
        )
        if not symbols:
            return

        self._ticks_seen[venue] = 0
        self._last_tick_at[venue] = time.monotonic()

        backoff = 1.0
        while True:
            exchange = None
            try:
                import ccxt.pro as ccxtpro  # lazy import — optional dep

                exchange_cls = getattr(ccxtpro, venue, None)
                if exchange_cls is None:
                    logger.warning("venue_not_found_in_ccxt", venue=venue)
                    return

                exchange = exchange_cls({"enableRateLimit": True})
                logger.info("spot_feed_connected", venue=venue, symbols=symbols)
                backoff = 1.0

                while True:
                    try:
                        tickers = await exchange.watch_tickers(symbols)
                        for sym, ticker in tickers.items():
                            price = float(ticker.get("last") or ticker.get("close") or 0)
                            if price > 0:
                                await self._record(venue, sym, price)
                    except asyncio.CancelledError:
                        raise  # propagate cancellation immediately
                    except Exception as inner_exc:
                        logger.warning(
                            "spot_feed_tick_error",
                            venue=venue,
                            error=str(inner_exc),
                        )
                        await asyncio.sleep(1.0)
                        break  # reconnect outer loop

            except asyncio.CancelledError:
                logger.info("spot_feed_cancelled", venue=venue)
                return  # clean exit on shutdown
            except ImportError:
                logger.error("ccxt_pro_not_installed", hint="pip install ccxt")
                return
            except Exception as exc:
                logger.exception("spot_feed_connect_error", venue=venue, error=str(exc))
            finally:
                # Always close exchange to release aiohttp sessions cleanly
                if exchange is not None:
                    try:
                        await exchange.close()
                    except Exception:
                        pass

            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _MAX_BACKOFF)

    async def _record(self, venue: str, symbol: str, price: float) -> None:
        # Build SpotTick first so its tz-aware `ts` (datetime via
        # utc_now()) is the single source of truth for both the
        # internal history sink AND the wire emit below — keeps the
        # storage timestamp and the broadcast timestamp identical.
        tick = SpotTick(
            venue=venue,  # type: ignore[arg-type]
            symbol=symbol,
            price=price,
        )
        async with self._lock:
            self._price_history[venue][symbol].append(price)
            self._last_ticks[venue][symbol] = tick
            self._ticks_seen[venue] = self._ticks_seen.get(venue, 0) + 1
            self._last_tick_at[venue] = time.monotonic()

        # v0.2.9 3i: fire-and-forget TICK WS broadcast, throttled per
        # (venue, symbol) stream at 250 ms. Runs OUTSIDE self._lock so
        # a slow WS subscriber can't stall the next watch_tickers loop
        # iteration. _record is already async (called via await from
        # _run_venue); the asyncio.create_task wrapping keeps the
        # broadcast itself non-blocking.
        self._maybe_emit_tick(venue, symbol, price, tick.ts)

    def _maybe_emit_tick(
        self,
        venue: str,
        symbol: str,
        price: float,
        ts: datetime,
    ) -> None:
        """Throttled fire-and-forget TICK broadcast (v0.2.9 3i).

        Per-stream cap: one frame per ``_TICK_EMIT_INTERVAL_SEC`` (250 ms
        per 3i policy lock 3). Silently no-ops if no Emitter is
        registered — matches the ``utils/fills.broadcast_fill`` precedent
        for headless / test environments. Logs ``tick_emit_started`` at
        INFO exactly once per (venue, symbol) per bot startup so the
        path's first firing is visible without ongoing log spam.
        """
        now_mono = time.monotonic()
        key = (venue, symbol)
        last_emit = self._last_emit.get(key, 0.0)
        if now_mono - last_emit < _TICK_EMIT_INTERVAL_SEC:
            return
        self._last_emit[key] = now_mono

        em = get_emitter()
        if em is None:
            return

        if key not in self._emit_logged:
            self._emit_logged.add(key)
            logger.info("tick_emit_started", venue=venue, symbol=symbol)

        try:
            wire = _build_price_tick(symbol, price, ts)
            asyncio.create_task(
                em.broadcast(
                    {"type": "TICK", "payload": wire.model_dump(mode="json")}
                )
            )
        except Exception as exc:  # noqa: BLE001
            # Defensive: feed loop MUST NOT die because of a malformed
            # tick or a downstream broadcast error.
            logger.warning(
                "tick_emit_failed",
                venue=venue,
                symbol=symbol,
                error=str(exc),
            )

    async def _heartbeat_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(60)
                now = time.monotonic()
                for venue in list(self._ticks_seen.keys()):
                    seconds_since = round(
                        now - self._last_tick_at.get(venue, now), 1
                    )
                    last_price = {
                        sym: tick.price
                        for sym, tick in self._last_ticks.get(venue, {}).items()
                    }
                    payload = dict(
                        venue=venue,
                        ticks_seen=self._ticks_seen[venue],
                        last_price=last_price,
                        seconds_since_last_tick=seconds_since,
                    )
                    if seconds_since > 30:
                        logger.warning("spot_feed_heartbeat", **payload)
                    else:
                        logger.info("spot_feed_heartbeat", **payload)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception(
                    "spot_feed_heartbeat_error",
                    error=str(exc),
                )

    # ------------------------------------------------------------------
    # Data access helpers
    # ------------------------------------------------------------------

    async def get_mid_price(self, symbol: str) -> float | None:
        """Return the average last price across venues, or None if unavailable."""
        async with self._lock:
            prices = [
                ticks[symbol].price
                for ticks in self._last_ticks.values()
                if symbol in ticks
            ]
        if not prices:
            return None
        return float(np.mean(prices))

    async def get_return_series(self, symbol: str) -> np.ndarray:
        """
        Return the most recent log-return series for a symbol,
        averaged across all available venues.
        """
        async with self._lock:
            all_prices: list[np.ndarray] = [
                np.asarray(hist[symbol], dtype=float)
                for hist in self._price_history.values()
                if symbol in hist and len(hist[symbol]) >= 2
            ]

        if not all_prices:
            return np.array([], dtype=float)

        # Use the shortest available series to align venues
        min_len = min(len(p) for p in all_prices)
        stacked = np.vstack([p[-min_len:] for p in all_prices])
        avg_prices = stacked.mean(axis=0)
        # Log returns: more statistically stable than simple returns
        log_returns = np.diff(np.log(np.clip(avg_prices, 1e-9, None)))
        return log_returns

    async def get_z_score(self, symbol: str, window: int = 60) -> float | None:
        """
        Compute rolling z-score of the most recent return vs window history.
        Returns None if insufficient data.
        """
        rets = await self.get_return_series(symbol)
        if len(rets) < max(window, 20):
            return None

        window_rets = rets[-window:]
        mean = np.mean(window_rets)
        std = np.std(window_rets)
        if std < 1e-10:
            return None

        z = float((window_rets[-1] - mean) / std)
        return z

    async def get_all_symbols(self) -> list[str]:
        """Return all symbols for which we have at least one price tick."""
        async with self._lock:
            symbols: set[str] = set()
            for ticks in self._last_ticks.values():
                symbols.update(ticks.keys())
        return sorted(symbols)
