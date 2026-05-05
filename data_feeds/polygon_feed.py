"""
data_feeds/polygon_feed.py
Optional Polygon RPC smart-money monitor.

Scans recent Polymarket trades on Polygon for wallets with high win-rate
and profit factor, exposing them for the SmartMoneyCopyStrategy.

DISABLED by default — enable via BOT_STRAT_SMART_MONEY_ENABLED=true.
Requires BOT_POLYGON_RPC_URL to be set.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import structlog

from config.settings import BotConfig

logger = structlog.get_logger(__name__)

_SCAN_INTERVAL_SEC = 5.0
_MAX_BACKOFF = 60.0


@dataclass(slots=True)
class WalletScore:
    wallet: str
    win_rate: float          # fraction 0–1
    profit_factor: float     # gross profit / gross loss
    total_trades: int = 0
    last_seen_block: int = 0


class PolygonSmartMoneyFeed:
    """
    Polls recent Polygon blocks for CTF Exchange events and scores wallets.

    NOTE: The actual wallet-scoring logic requires your own indexed dataset
    (e.g. a subgraph or off-chain PnL database keyed by wallet address).
    The scaffold below shows the interface and filtering logic — replace
    `_scan_recent_wallets` with your indexer call.
    """

    def __init__(self, config: BotConfig) -> None:
        self._config = config
        self._scores: dict[str, WalletScore] = {}
        self._w3: object | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    async def _initialize(self) -> bool:
        """Connect to Polygon RPC. Returns True on success."""
        rpc = self._config.api.polygon_rpc_url
        if not rpc:
            logger.warning("polygon_rpc_url_not_set")
            return False
        try:
            from web3 import AsyncWeb3
            from web3.providers.async_rpc import AsyncHTTPProvider

            w3 = AsyncWeb3(AsyncHTTPProvider(rpc))
            chain = await w3.eth.chain_id
            expected = self._config.api.chain_id
            if chain != expected:
                logger.warning(
                    "polygon_unexpected_chain",
                    got=chain,
                    expected=expected,
                )
                return False
            self._w3 = w3
            logger.info("polygon_feed_connected", chain_id=chain)
            return True
        except ImportError:
            logger.error("web3_not_installed", hint="pip install web3")
            return False
        except Exception as exc:
            logger.exception("polygon_init_error", error=str(exc))
            return False

    # ------------------------------------------------------------------
    # Run loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        if not self._config.strategy.smart_money_enabled:
            logger.info("polygon_feed_disabled")
            return

        if not await self._initialize():
            return

        backoff = _SCAN_INTERVAL_SEC
        while True:
            try:
                await self._scan_recent_wallets()
                await self._evict_below_threshold()
                backoff = _SCAN_INTERVAL_SEC
            except Exception as exc:
                logger.exception("polygon_scan_error", error=str(exc))
                backoff = min(backoff * 2, _MAX_BACKOFF)

            await asyncio.sleep(backoff)

    # ------------------------------------------------------------------
    # Wallet scoring (stub — replace with your indexer)
    # ------------------------------------------------------------------

    # Note: BotStrategyConfig refuses to enable smart_money_enabled
    # until this stub is replaced — see
    # config/settings.py:_smart_money_indexer_check.

    async def _scan_recent_wallets(self) -> None:
        """
        Fetch recent wallet PnL data and upsert scores.

        Replace this with a call to your subgraph / off-chain DB.
        Expected data shape per wallet:
            {
                "wallet": "0xABC...",
                "win_rate": 0.72,
                "profit_factor": 2.1,
                "total_trades": 85,
                "last_seen_block": 65_000_000,
            }
        """
        # --- STUB: wire your data source here ---
        # Example (pseudocode):
        #   rows = await my_indexer.get_top_wallets(limit=50)
        #   for row in rows:
        #       await self._upsert(row)
        pass

    async def _upsert(self, data: dict) -> None:
        wallet = str(data.get("wallet", ""))
        if not wallet:
            return
        async with self._lock:
            self._scores[wallet] = WalletScore(
                wallet=wallet,
                win_rate=float(data.get("win_rate", 0)),
                profit_factor=float(data.get("profit_factor", 0)),
                total_trades=int(data.get("total_trades", 0)),
                last_seen_block=int(data.get("last_seen_block", 0)),
            )

    async def _evict_below_threshold(self) -> None:
        """Remove wallets that no longer meet quality filters."""
        sc = self._config.strategy
        async with self._lock:
            to_remove = [
                w for w, score in self._scores.items()
                if score.win_rate < sc.smart_money_min_win_rate
                or score.profit_factor < sc.smart_money_min_profit_factor
            ]
            for w in to_remove:
                del self._scores[w]
        if to_remove:
            logger.debug("polygon_evicted_wallets", count=len(to_remove))

    # ------------------------------------------------------------------
    # Public accessor
    # ------------------------------------------------------------------

    def get_qualified_wallets(self) -> list[WalletScore]:
        """Return current qualified wallet list (non-blocking snapshot)."""
        return list(self._scores.values())

