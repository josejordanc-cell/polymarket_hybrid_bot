"""
main.py
Async runtime: feeds → warmup → risk gating → strategy priority loop → execution.

Strategy priority (strict):
  1. IntraArbitrageStrategy    — risk-free guaranteed edge
  2. MomentumLatencyStrategy   — spot-lag signals on 5m/15m markets
  3. MarketMakerStrategy       — spread capture / rebates
  4. SmartMoneyCopyStrategy    — optional, disabled by default

Run simulation (safe default):
    python main.py
    python main.py --sim

Run live (requires all API credentials in .env):
    python main.py --live
"""
from __future__ import annotations

import argparse
import asyncio
import os
import signal
import time

# Logging configured before any other local import
from utils.logging import configure_logging

_SHUTDOWN = asyncio.Event()

# How long to wait for feeds to receive first data before starting strategies
_FEED_WARMUP_SEC = 5.0
# Minimum markets required to start (warn but continue if 0)
_MIN_MARKETS_WARN = 1
# Log strategy loop activity at most once every N seconds per market (noise reduction)
_SIGNAL_LOG_THROTTLE_SEC = 10.0


def _install_signal_handlers() -> None:
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _SHUTDOWN.set)
        except NotImplementedError:
            pass  # Windows


async def _interruptible_sleep(shutdown: asyncio.Event, seconds: float) -> bool:
    """Wait up to `seconds`. Return True if `shutdown` was set during the wait."""
    try:
        await asyncio.wait_for(shutdown.wait(), timeout=seconds)
        return True
    except asyncio.TimeoutError:
        return False


# IMPLEMENTATION NOTE — REST polling is intentional for v1 simplicity.
# If the dashboard requires resolution latency below ~60s, REPLACE this REST
# poll with a Polygon contract listener that subscribes to UMA/CTF resolution
# events. See HANDOFF.md P3 backlog. Do not just lower the poll interval —
# Polymarket REST will rate-limit before you get useful latency improvements.
async def resolution_polling_loop(
    risk,
    config,
    shutdown: asyncio.Event,
) -> None:
    """Periodically poll Gamma REST for resolved markets and call RiskManager.on_settlement."""
    from data_feeds.polymarket_feed import poll_market_resolutions

    import structlog

    from observability.metrics_exporter import get_metrics_exporter
    from utils.alerts import send_alert

    logger = structlog.get_logger(__name__)
    base_interval = float(config.resolution_poll_seconds)
    consecutive_failures = 0
    last_error = ""

    while not shutdown.is_set():
        try:
            market_ids = list({p.market_id for p in risk.open_positions.values()})
            if not market_ids:
                consecutive_failures = 0
                if await _interruptible_sleep(shutdown, base_interval):
                    break
                continue

            events = await poll_market_resolutions(market_ids)
            consecutive_failures = 0

            for ev in events:
                if not any(p.market_id == ev.market_id for p in risk.open_positions.values()):
                    continue
                eq_before = risk.equity
                risk.on_settlement(ev.market_id, ev.resolved_outcome)
                eq_after = risk.equity
                pnl = eq_after - eq_before
                logger.info(
                    "market_resolved",
                    market_id=ev.market_id,
                    outcome=ev.resolved_outcome,
                    pnl_realised=round(pnl, 6),
                    equity_after=round(eq_after, 6),
                )

            if await _interruptible_sleep(shutdown, base_interval):
                break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            consecutive_failures += 1
            last_error = str(exc)
            logger.warning(
                "resolution_poll_failed",
                error=last_error,
                consecutive_failures=consecutive_failures,
            )
            if consecutive_failures >= 5:
                logger.error(
                    "resolution_polling_degraded",
                    error=last_error,
                    consecutive_failures=consecutive_failures,
                )
                await send_alert(
                    f"Resolution polling degraded after {consecutive_failures} failures: {last_error}",
                    level="critical",
                )
                mx = get_metrics_exporter()
                if mx:
                    mx.record_resolution_poll_failure()

            backoff_sec = min(60.0 * (2 ** (consecutive_failures - 1)), 600.0)
            if await _interruptible_sleep(shutdown, backoff_sec):
                break


async def equity_reconciliation_loop(
    risk,
    config,
    shutdown: asyncio.Event,
) -> None:
    """Periodically compare RiskManager.equity to Polygon wallet USDC (read-only RPC)."""
    import structlog
    from web3 import Web3

    from observability.metrics_exporter import get_metrics_exporter
    from utils.alerts import send_alert
    from utils.equity_reconciliation import (
        compute_drift_pct,
        get_polygon_wallet_address,
        maybe_alert_equity_drift,
        resolve_polygon_usdc_human,
    )

    logger = structlog.get_logger(__name__)
    base_interval = float(config.reconciliation_interval_seconds)
    consecutive_failures = 0
    last_error = ""

    rpc = (config.api.polygon_rpc_url or "").strip()
    if not rpc:
        logger.warning(
            "equity_reconciliation_disabled",
            reason="BOT_POLYGON_RPC_URL unset",
        )
        while not shutdown.is_set():
            if await _interruptible_sleep(shutdown, max(base_interval, 60.0)):
                break
        return

    w3 = Web3(Web3.HTTPProvider(rpc))
    try:
        wallet = get_polygon_wallet_address(config)
    except Exception as exc:
        logger.warning(
            "equity_reconciliation_disabled",
            reason="wallet_address_error",
            error=str(exc),
        )
        while not shutdown.is_set():
            if await _interruptible_sleep(shutdown, max(base_interval, 60.0)):
                break
        return

    def _fetch_bal() -> tuple[float, str]:
        def _warn_both(_msg: str, fields: dict) -> None:
            logger.warning("usdc_both_bridged_and_native", **fields)

        return resolve_polygon_usdc_human(w3, wallet, warn=_warn_both)

    while not shutdown.is_set():
        try:
            on_chain_usdc, usdc_src = await asyncio.to_thread(_fetch_bal)
            consecutive_failures = 0

            bot_equity = risk.equity
            drift_usd, drift_pct = compute_drift_pct(on_chain_usdc, bot_equity)

            logger.info(
                "equity_reconciled",
                bot_equity=round(bot_equity, 6),
                on_chain_usdc=round(on_chain_usdc, 6),
                drift_usd=round(drift_usd, 6),
                drift_pct=round(drift_pct, 6),
                usdc_source=usdc_src,
            )

            mx = get_metrics_exporter()
            if mx:
                mx.update_drift(drift_pct)

            thresh = float(config.reconciliation_drift_threshold_pct)
            if abs(drift_pct) > thresh:
                logger.warning(
                    "equity_drift_alert",
                    drift_pct=round(drift_pct, 4),
                    threshold_pct=thresh,
                    drift_usd=round(drift_usd, 4),
                    on_chain_usdc=round(on_chain_usdc, 4),
                    bot_equity=round(bot_equity, 4),
                )
                await maybe_alert_equity_drift(
                    drift_pct=drift_pct,
                    drift_usd=drift_usd,
                    on_chain_usdc=on_chain_usdc,
                    bot_equity_usd=bot_equity,
                    threshold_pct=thresh,
                    send_alert=send_alert,
                )

            if await _interruptible_sleep(shutdown, base_interval):
                break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            consecutive_failures += 1
            last_error = str(exc)
            logger.warning(
                "equity_reconciliation_failed",
                error=last_error,
                consecutive_failures=consecutive_failures,
            )
            if consecutive_failures >= 5:
                logger.error(
                    "equity_reconciliation_degraded",
                    error=last_error,
                    consecutive_failures=consecutive_failures,
                )
                await send_alert(
                    f"Equity reconciliation degraded after {consecutive_failures} "
                    f"RPC failures: {last_error}",
                    level="critical",
                )
            backoff_sec = min(
                base_interval * (2 ** (consecutive_failures - 1)),
                3600.0,
            )
            if await _interruptible_sleep(shutdown, backoff_sec):
                break


# ---------------------------------------------------------------------------
# Main coroutine
# ---------------------------------------------------------------------------

async def run_bot(mode: str) -> None:
    # ── Set env var BEFORE get_config() so lru_cache sees the right mode ──
    os.environ["BOT_MODE"] = mode

    # All local imports deferred until after env is set
    from config import get_config
    from data_feeds.binance_feed import BinanceFeed
    from data_feeds.polygon_feed import PolygonSmartMoneyFeed
    from data_feeds.polymarket_feed import PolymarketFeed, discover_liquid_markets
    from execution.clob_executor import CLOBExecutor
    from risk_manager import RiskManager
    from strategies.intra_arbitrage import IntraArbitrageStrategy, reconcile_arb_bundle
    from strategies.market_maker import MarketMakerStrategy
    from strategies.momentum_latency import MomentumLatencyStrategy
    from strategies.smart_money_copy import SmartMoneyCopyStrategy
    from utils.alerts import send_alert
    from utils.fill_correlation import record_fill_return_for_correlation
    from utils.types import OrderIntent, TradeResult

    config = get_config()
    configure_logging(config.runtime.log_level)

    import structlog

    structlog.contextvars.bind_contextvars(
        experiment_id=config.experiment_id,
        mode=config.runtime.mode,
    )

    logger = structlog.get_logger(__name__)

    metrics = None
    if config.metrics_enabled:
        from observability.metrics_exporter import (
            MetricsExporter,
            notify_fill_metrics,
            set_metrics_exporter,
        )

        metrics = MetricsExporter(
            experiment_id=config.experiment_id,
            mode=config.runtime.mode,
        )
        metrics.start(config.metrics_port)
        set_metrics_exporter(metrics)
    else:
        from observability.metrics_exporter import notify_fill_metrics, set_metrics_exporter

        set_metrics_exporter(None)

    logger.info("bot_starting", mode=config.runtime.mode, version="v3")
    await send_alert(
        f"Bot starting in {config.runtime.mode.upper()} mode", level="info"
    )

    # ── Market discovery (cached) ──────────────────────────────────────────
    markets = await _load_or_discover_markets(config, logger)
    if metrics:
        metrics.update_active_markets(len(markets))
    # WS subscription uses token IDs (yes + no), not condition IDs
    # The WS sends asset_id = token_id in price events
    market_ids = []
    for m in markets:
        if m.get("yes_token_id"):
            market_ids.append(m["yes_token_id"])
        if m.get("no_token_id"):
            market_ids.append(m["no_token_id"])
    # Keep condition_id→market mapping for strategy lookup
    _condition_to_market = {m["market_id"]: m for m in markets if m["market_id"]}

    if len(market_ids) < _MIN_MARKETS_WARN:
        logger.warning(
            "no_markets_discovered",
            hint="Lower BOT_STRAT_MIN_LIQUIDITY_USD or add market IDs manually. "
                 "Bot will run but strategy loop will idle.",
        )
        await send_alert("⚠️ No markets discovered — bot idling", level="warning")

    # ── Feeds ──────────────────────────────────────────────────────────────
    pm_feed = PolymarketFeed(config, market_ids=market_ids)
    spot_feed = BinanceFeed(config)
    polygon_feed = PolygonSmartMoneyFeed(config)

    # ── Risk + strategies ──────────────────────────────────────────────────
    risk = RiskManager(config)
    if metrics:
        metrics.update_equity(risk.equity)
        metrics.update_open_positions(len(risk.open_positions))
        metrics.set_kill_switch(risk.kill_switch_active())

    arb = IntraArbitrageStrategy(config)
    mom = MomentumLatencyStrategy(config)
    mm = MarketMakerStrategy(config)
    sm = SmartMoneyCopyStrategy(config)
    executor = CLOBExecutor(config)

    # ── Start background feed tasks ────────────────────────────────────────
    tasks = [
        asyncio.create_task(pm_feed.run(), name="polymarket_feed"),
        asyncio.create_task(spot_feed.run(), name="spot_feed"),
        asyncio.create_task(polygon_feed.run(), name="polygon_feed"),
        asyncio.create_task(
            resolution_polling_loop(risk, config, _SHUTDOWN),
            name="resolution_poll",
        ),
        asyncio.create_task(
            equity_reconciliation_loop(risk, config, _SHUTDOWN),
            name="equity_reconciliation",
        ),
        asyncio.create_task(_SHUTDOWN.wait(), name="shutdown_watcher"),
    ]

    # ── Feed warmup: wait for first data before firing strategies ──────────
    logger.info("feed_warmup_start", seconds=_FEED_WARMUP_SEC)
    await _wait_for_feeds(spot_feed, _FEED_WARMUP_SEC, logger)
    logger.info("feed_warmup_complete")

    # Pre-build symbol map
    symbol_map: dict[str, str] = _build_symbol_map(markets)

    # Throttle: track last log time per market to reduce noise
    _last_signal_log: dict[str, float] = {}

    logger.info(
        "strategy_loop_started",
        markets=len(market_ids),
        mode=config.runtime.mode,
    )

    try:
        while not _SHUTDOWN.is_set():
            if metrics:
                metrics.set_kill_switch(risk.kill_switch_active())

            if risk.kill_switch_active():
                await asyncio.sleep(5.0)
                continue

            snapshots = await pm_feed.get_all_snapshots()
            if metrics:
                metrics.update_active_markets(len(snapshots))
            smart_wallets = polygon_feed.get_qualified_wallets()

            for snap in snapshots.values():
                intents: list[OrderIntent] = []
                signal_name: str = ""

                # ── Priority 1: Intra-market arbitrage ─────────────────────
                arb_sig = arb.find_signal(snap)
                if arb_sig:
                    size_usd = risk.kelly_size_usd(arb_sig)
                    if size_usd > 0:
                        intents = arb.build_orders(snap, size_usd)
                        signal_name = "arb"

                # ── Priority 2: Momentum / latency edge ────────────────────
                if not intents:
                    sym = _resolve_symbol(snap.market_id, symbol_map, markets)
                    ret_series = await spot_feed.get_return_series(sym)
                    mom_sig = mom.find_signal(snap, sym, ret_series)
                    if mom_sig:
                        size_usd = risk.kelly_size_usd(mom_sig)
                        if size_usd > 0:
                            order = mom.build_order(mom_sig, size_usd, snap)
                            if order:
                                intents = [order]
                                signal_name = "momentum"

                # ── Priority 3: Market making ──────────────────────────────
                if not intents:
                    mm_size = config.risk.bankroll_usd * 0.01
                    intents = mm.build_quotes(snap, mm_size)
                    if intents:
                        signal_name = "mm"

                # ── Priority 4 (optional): Smart-money copy ────────────────
                if config.strategy.smart_money_enabled:
                    sm_sig = sm.find_signal(snap, smart_wallets)
                    if sm_sig:
                        size_usd = risk.kelly_size_usd(sm_sig)
                        if size_usd > 0:
                            order = sm.build_order(sm_sig, size_usd, snap)
                            if order:
                                intents.append(order)
                                signal_name = signal_name or "smart_money"

                # ── Throttled signal logging (reduce noise) ────────────────
                now = time.monotonic()
                last = _last_signal_log.get(snap.market_id, 0.0)
                if signal_name and (now - last) >= _SIGNAL_LOG_THROTTLE_SEC:
                    logger.debug(
                        "signal_active",
                        market=snap.market_id[:12],
                        strategy=signal_name,
                        intents=len(intents),
                    )
                    _last_signal_log[snap.market_id] = now

                # ── Risk approval + execution ──────────────────────────────
                approved = risk.approve_orders(intents)

                async def _exec_and_record(intent: OrderIntent) -> TradeResult:
                    result = await executor.execute(intent)
                    if result.accepted and result.filled_size > 0:
                        risk.on_fill(
                            intent,
                            fill_price=result.fill_price or intent.price_rounded(),
                            filled_size=result.filled_size,
                        )
                        notify_fill_metrics(
                            intent.strategy,
                            intent.side,
                            intent.tif,
                            result.fill_price,
                            result.filled_size,
                        )
                        record_fill_return_for_correlation(
                            risk,
                            intent,
                            snap,
                            result.fill_price,
                            result.filled_size,
                        )
                    if result.accepted:
                        mm.on_fill(intent, result.filled_size)

                    if result.accepted or intent.strategy != "market_maker":
                        logger.info(
                            "trade_result",
                            strategy=intent.strategy,
                            market=intent.market_id[:12],
                            outcome=intent.outcome,
                            accepted=result.accepted,
                            fill_price=result.fill_price,
                            size=result.filled_size,
                            reason=result.reason,
                        )
                    return result

                def _schedule_arb_reconcile(
                    bundle_id_arg: str,
                    yi: OrderIntent,
                    ni: OrderIntent,
                    ry: TradeResult,
                    rn: TradeResult,
                ) -> None:
                    async def _run() -> None:
                        try:
                            await reconcile_arb_bundle(
                                config,
                                executor,
                                snap,
                                bundle_id_arg,
                                yi,
                                ni,
                                ry,
                                rn,
                                risk,
                                mm,
                            )
                        except Exception:
                            logger.exception(
                                "arb_reconcile_failed",
                                bundle_id=bundle_id_arg,
                            )

                    asyncio.create_task(_run())

                idx = 0
                while idx < len(approved):
                    cur = approved[idx]
                    nxt = approved[idx + 1] if idx + 1 < len(approved) else None
                    if (
                        cur.strategy == "intra_arbitrage"
                        and cur.bundle_id
                        and nxt is not None
                        and nxt.strategy == "intra_arbitrage"
                        and nxt.bundle_id == cur.bundle_id
                    ):
                        yes_intent = cur if cur.outcome == "YES" else nxt
                        no_intent = nxt if cur.outcome == "YES" else cur
                        r_yes = await _exec_and_record(yes_intent)
                        r_no = await _exec_and_record(no_intent)
                        _schedule_arb_reconcile(
                            str(yes_intent.bundle_id or ""),
                            yes_intent,
                            no_intent,
                            r_yes,
                            r_no,
                        )
                        idx += 2
                        continue

                    await _exec_and_record(cur)
                    idx += 1

            await asyncio.sleep(config.runtime.strategy_interval_sec)

    finally:
        from observability.metrics_exporter import set_metrics_exporter

        set_metrics_exporter(None)
        logger.info("bot_shutting_down", equity=round(risk.equity, 2))
        await send_alert(
            f"Bot shutting down | equity=${risk.equity:,.2f}", level="warning"
        )
        # Cancel all background tasks and wait for clean exit
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # Give aiohttp sessions a moment to close cleanly
        await asyncio.sleep(0.5)
        _log_session_summary(executor, logger)


# ---------------------------------------------------------------------------
# Feed warmup helper
# ---------------------------------------------------------------------------

async def _wait_for_feeds(spot_feed, timeout: float, logger) -> None:
    """Wait up to `timeout` seconds for the spot feed to receive first data."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        syms = await spot_feed.get_all_symbols()
        if syms:
            logger.info("spot_feed_ready", symbols=syms)
            return
        await asyncio.sleep(0.5)
    logger.warning(
        "spot_feed_warmup_timeout",
        seconds=timeout,
        hint="Spot feed not receiving data yet — strategies will still start",
    )


# ---------------------------------------------------------------------------
# Market discovery with cache
# ---------------------------------------------------------------------------

async def _load_or_discover_markets(config, logger) -> list[dict]:
    import json
    import time as _time
    from pathlib import Path

    cache_path = Path(".market_cache.json")
    cache_ttl = 3600  # 1 hour

    if cache_path.exists():
        age = _time.time() - cache_path.stat().st_mtime
        if age < cache_ttl:
            try:
                markets = json.loads(cache_path.read_text())
                logger.info(
                    "market_cache_hit",
                    count=len(markets),
                    age_min=round(age / 60, 1),
                )
                return markets
            except Exception as exc:
                logger.warning("market_cache_corrupt", error=str(exc))

    from data_feeds.polymarket_feed import discover_liquid_markets

    logger.info("market_discovery_api_call")
    try:
        markets = await discover_liquid_markets(
            host=config.api.polymarket_host,
            min_liquidity_usd=500.0,  # low threshold for discovery; strategies filter further
            tags=[
                # Crypto markets
                "bitcoin", "btc", "ethereum", "eth", "solana", "sol", "crypto",
                # High-liquidity binary markets with good momentum properties
                "ceasefire", "fed", "interest rate", "trump", "will the fed",
                "will trump", "rate", "bps",
            ],
        )
    except Exception as exc:
        logger.exception("market_discovery_failed", error=str(exc))
        markets = []

    if markets:
        try:
            cache_path.write_text(json.dumps(markets))
            logger.info("market_cache_written", count=len(markets))
        except Exception as exc:
            logger.warning("market_cache_write_failed", error=str(exc))
    else:
        logger.warning(
            "market_discovery_empty",
            hint="API returned no markets. Cache not updated.",
        )

    return markets


# ---------------------------------------------------------------------------
# Symbol helpers
# ---------------------------------------------------------------------------

def _build_symbol_map(markets: list[dict]) -> dict[str, str]:
    return {
        m["market_id"]: _infer_symbol_from_question(m.get("question", ""))
        for m in markets
        if m.get("market_id")
    }


def _resolve_symbol(
    market_id: str,
    symbol_map: dict[str, str],
    markets: list[dict],
) -> str:
    """Return cached symbol or infer from markets list; never returns None."""
    if market_id in symbol_map:
        return symbol_map[market_id]
    for m in markets:
        if m.get("market_id") == market_id:
            sym = _infer_symbol_from_question(m.get("question", ""))
            symbol_map[market_id] = sym  # cache for next iteration
            return sym
    symbol_map[market_id] = "BTC/USDT"  # safe default
    return "BTC/USDT"


def _infer_symbol_from_question(question: str) -> str:
    q = question.lower()
    if "eth" in q or "ethereum" in q:
        return "ETH/USDT"
    if "sol" in q or "solana" in q:
        return "SOL/USDT"
    return "BTC/USDT"


# ---------------------------------------------------------------------------
# Session summary
# ---------------------------------------------------------------------------

def _log_session_summary(executor, logger) -> None:
    try:
        summary = executor._sim.summary()  # noqa: SLF001
        logger.info("session_summary", **summary)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Polymarket hybrid trading bot v3")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--sim", action="store_true", default=True,
        help="Paper-trading simulation (default, safe)",
    )
    group.add_argument(
        "--live", action="store_true",
        help="Live trading — requires all API credentials in .env",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    mode = "live" if args.live else "sim"
    _install_signal_handlers()
    asyncio.run(run_bot(mode))
