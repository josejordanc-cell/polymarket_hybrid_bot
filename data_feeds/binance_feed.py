"""
data_feeds/binance_feed.py
Real-time spot price feed for BTC, ETH, SOL via ccxt.pro WebSocket.

Maintains a rolling return history per symbol for momentum/z-score strategies.
"""
from __future__ import annotations

import asyncio
import os
from collections import defaultdict, deque

import numpy as np
import structlog

from config.settings import BotConfig
from utils.types import SpotTick

logger = structlog.get_logger(__name__)

_MAX_BACKOFF = 30.0
# Number of ticks retained per symbol for z-score computation
_HISTORY_LEN = 500


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
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Run loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        tasks = [
            asyncio.create_task(self._run_venue("binance"), name="binance_spot"),
            asyncio.create_task(self._run_venue("coinbase"), name="coinbase_spot"),
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
        async with self._lock:
            self._price_history[venue][symbol].append(price)
            self._last_ticks[venue][symbol] = SpotTick(
                venue=venue,  # type: ignore[arg-type]
                symbol=symbol,
                price=price,
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
