from __future__ import annotations

import asyncio

import ccxt.pro as ccxt_pro
import structlog

from config.settings import BotConfig
from utils.types import SpotTick

logger = structlog.get_logger(__name__)


class SpotFeed:
    def __init__(self, config: BotConfig) -> None:
        self._config = config
        self._last_ticks: dict[str, SpotTick] = {}
        self._lock = asyncio.Lock()

    async def run(self) -> None:
        await asyncio.gather(self._run_binance(), self._run_coinbase())

    async def _run_binance(self) -> None:
        exchange = ccxt_pro.binance({"enableRateLimit": True})
        symbol = self._config.api.binance_symbol
        while True:
            try:
                ticker = await exchange.watch_ticker(symbol)
                await self._save_tick("binance", symbol, float(ticker["last"]))
            except Exception as exc:
                logger.exception("binance_feed_error", error=str(exc))
                await asyncio.sleep(1.0)

    async def _run_coinbase(self) -> None:
        exchange = ccxt_pro.coinbase({"enableRateLimit": True})
        symbol = self._config.api.coinbase_symbol
        while True:
            try:
                ticker = await exchange.watch_ticker(symbol)
                await self._save_tick("coinbase", symbol, float(ticker["last"]))
            except Exception as exc:
                logger.exception("coinbase_feed_error", error=str(exc))
                await asyncio.sleep(1.0)

    async def _save_tick(self, venue: str, symbol: str, price: float) -> None:
        async with self._lock:
            self._last_ticks[venue] = SpotTick(venue=venue, symbol=symbol, price=price)

    async def get_mid_price(self) -> float | None:
        async with self._lock:
            if "binance" not in self._last_ticks and "coinbase" not in self._last_ticks:
                return None
            prices = [tick.price for tick in self._last_ticks.values()]
            return sum(prices) / len(prices)
