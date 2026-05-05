"""
config/settings.py
Pydantic-v2 settings for the Polymarket hybrid trading bot.
All values are env-driven (prefix BOT_) with safe simulation defaults.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# ---------------------------------------------------------------------------
# Sub-configs
# ---------------------------------------------------------------------------

class ApiConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="BOT_", extra="ignore"
    )

    polymarket_host: str = Field(default="https://clob.polymarket.com")
    polymarket_ws_url: str = Field(
        default="wss://ws-subscriptions-clob.polymarket.com/ws/market"
    )
    # Credentials — must be non-empty in live mode (validated by BotConfig)
    polymarket_private_key: str = Field(default="")
    polymarket_api_key: str = Field(default="")
    polymarket_api_secret: str = Field(default="")
    polymarket_api_passphrase: str = Field(default="")

    # Spot feed symbols
    binance_symbols: list[str] = Field(default=["BTC/USDT", "ETH/USDT", "SOL/USDT"])
    coinbase_symbols: list[str] = Field(default=["BTC/USD", "ETH/USD", "SOL/USD"])

    # Polygon / smart-money
    polygon_rpc_url: str = Field(default="")
    chain_id: int = Field(default=137)


class RiskConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="BOT_RISK_", extra="ignore"
    )

    bankroll_usd: float = Field(default=10_000.0, gt=0)
    # Per-trade cap: 5–10 % of equity
    max_trade_pct: float = Field(default=0.08, ge=0.05, le=0.10)
    # Daily drawdown kill-switch (fraction, e.g. 0.12 = 12 %)
    daily_drawdown_limit_pct: float = Field(default=0.12, ge=0.03, le=0.30)
    kelly_fraction: float = Field(
        default=0.25,
        ge=0.05,
        le=0.50,
        description=(
            "Kelly multiplier; quarter-Kelly default. Hard cap 0.50; "
            "full Kelly is rejected as operationally ruinous."
        ),
    )
    # Correlation gate: block new trades when |corr| > threshold
    max_pair_correlation: float = Field(default=0.80, ge=0.0, le=1.0)
    # Maximum simultaneous open positions
    max_open_positions: int = Field(default=8, ge=1, le=50)
    # Minimum notional per order (prevents dust trades)
    min_order_usd: float = Field(default=10.0, gt=0)


class StrategyConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="BOT_STRAT_", extra="ignore"
    )

    momentum_enabled: bool = Field(default=True)
    market_maker_enabled: bool = Field(default=True)
    smart_money_enabled: bool = Field(default=False)

    # Smart-money copy filter thresholds
    smart_money_min_win_rate: float = Field(default=0.60, ge=0.50, le=1.0)
    smart_money_min_profit_factor: float = Field(default=1.5, ge=1.0, le=4.0)

    # Market liquidity gate
    min_liquidity_usd: float = Field(default=5_000.0, ge=0)
    # Arbitrage: minimum guaranteed edge to enter (avoids fee erosion)
    min_arb_edge: float = Field(default=0.005, ge=0.001, le=0.05)
    # Max implied loss (bps vs entry) to accept when unwinding one-sided arb fills; also caps IOC SELL limit slippage from touch
    arb_max_unwind_bps: int = Field(default=50, ge=1, le=5000)
    # Momentum: z-score threshold before signalling
    momentum_z_threshold: float = Field(default=2.0, ge=0.5, le=10.0)
    # Market-making spread in basis points
    mm_spread_bps: int = Field(default=40, ge=5, le=500)
    # Momentum window size (number of ticks)
    momentum_window: int = Field(default=60, ge=20, le=500)

    @model_validator(mode="after")
    def _smart_money_indexer_check(self) -> "StrategyConfig":
        if self.smart_money_enabled:
            # data_feeds/polygon_feed.py:_scan_recent_wallets is a stub.
            # Until that's wired to a real indexer, refuse to enable.
            raise ValueError(
                "BOT_STRAT_SMART_MONEY_ENABLED=true but the wallet "
                "indexer (data_feeds/polygon_feed.py:_scan_recent_wallets)"
                " is a stub. Implement that function before enabling "
                "this strategy."
            )
        return self


class RuntimeConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="BOT_", extra="ignore"
    )

    mode: Literal["sim", "live"] = Field(default="sim")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(default="INFO")
    poll_interval_sec: float = Field(default=0.25, ge=0.05, le=5.0)
    strategy_interval_sec: float = Field(default=0.20, ge=0.05, le=5.0)
    # Simulator slippage in bps
    sim_slippage_bps: int = Field(default=5, ge=0, le=100)

    @field_validator("mode")
    @classmethod
    def _validate_mode(cls, v: str) -> str:
        if v not in {"sim", "live"}:
            raise ValueError("mode must be 'sim' or 'live'")
        return v


# ---------------------------------------------------------------------------
# Composite root config
# ---------------------------------------------------------------------------

class BotConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    api: ApiConfig = Field(default_factory=ApiConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    strategy: StrategyConfig = Field(default_factory=StrategyConfig)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)

    experiment_id: str = Field(
        default="default",
        validation_alias="BOT_EXPERIMENT_ID",
    )

    resolution_poll_seconds: float = Field(
        default=60.0,
        ge=5.0,
        le=86_400.0,
        validation_alias="BOT_RESOLUTION_POLL_SECONDS",
    )

    metrics_enabled: bool = Field(
        default=True,
        validation_alias="BOT_METRICS_ENABLED",
    )
    metrics_port: int = Field(
        default=9091,
        ge=1,
        le=65535,
        validation_alias="BOT_METRICS_PORT",
    )

    # Live CLOB fill reconciliation (REST fallback when user WS is unavailable in SDK)
    # REST fallback only (py-clob-client has no user/fill WebSocket helper today).
    fill_poll_interval_sec: float = Field(
        default=2.0,
        ge=0.1,
        le=60.0,
        validation_alias="BOT_FILL_POLL_INTERVAL_SEC",
    )
    fill_poll_timeout_sec: float = Field(
        default=60.0,
        ge=0.1,
        le=3600.0,
        validation_alias="BOT_FILL_POLL_TIMEOUT_SEC",
    )

    reconciliation_interval_seconds: int = Field(
        default=3600,
        ge=10,
        le=86_400,
        validation_alias="BOT_RECONCILIATION_INTERVAL_SECONDS",
    )
    reconciliation_drift_threshold_pct: float = Field(
        default=1.0,
        gt=0,
        le=500.0,
        validation_alias="BOT_RECONCILIATION_DRIFT_THRESHOLD_PCT",
    )
    polygon_wallet_address: str = Field(
        default="",
        validation_alias="BOT_POLYGON_WALLET_ADDRESS",
    )

    market_cache_path: Path = Field(
        default=Path("/app/data/market_cache.json"),
        description=(
            "Filesystem path for the markets discovery cache. "
            "Parent directory will be auto-created on write."
        ),
        validation_alias="BOT_MARKET_CACHE_PATH",
    )

    @model_validator(mode="after")
    def _live_mode_requires_keys(self) -> "BotConfig":
        if self.runtime.mode == "live":
            missing = [
                name
                for name, val in {
                    "polymarket_private_key": self.api.polymarket_private_key,
                    "polymarket_api_key": self.api.polymarket_api_key,
                    "polymarket_api_secret": self.api.polymarket_api_secret,
                    "polymarket_api_passphrase": self.api.polymarket_api_passphrase,
                }.items()
                if not val
            ]
            if missing:
                raise ValueError(
                    f"Live mode requires these env vars to be set: {missing}"
                )
        return self


@lru_cache(maxsize=1)
def get_config() -> BotConfig:
    """Return the singleton config (cached after first call)."""
    return BotConfig()


# Convenience alias
Settings = BotConfig
