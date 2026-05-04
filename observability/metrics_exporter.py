"""
Prometheus /metrics exposition (prometheus_client).

When the bot runs with BOT_METRICS_ENABLED=false, do not instantiate this class;
call sites use get_metrics_exporter() which returns None and imports nothing heavy.
"""
from __future__ import annotations

import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from prometheus_client import CollectorRegistry

_metrics_exporter: MetricsExporter | None = None


def set_metrics_exporter(exporter: MetricsExporter | None) -> None:
    global _metrics_exporter
    _metrics_exporter = exporter


def get_metrics_exporter() -> MetricsExporter | None:
    return _metrics_exporter


def notify_fill_metrics(
    strategy: str,
    side: str,
    tif: str,
    fill_price: float | None,
    filled_size: float,
) -> None:
    """
    Increment Prometheus fill counters after a reconciled fill (live or sim).
    Call once per fill after RiskManager.on_fill (Prompt 2 / Prompt 8 alignment).
    """
    if filled_size <= 0:
        return
    mx = get_metrics_exporter()
    if not mx:
        return
    fp = float(fill_price or 0.0)
    mx.record_fill(strategy, side, tif, abs(fp * filled_size))


class MetricsExporter:
    """
    HTTP /metrics server (background thread) and typed updates for bot gauges/counters.

    Every metric includes static labels experiment_id and mode (sim/live).
    """

    def __init__(
        self,
        experiment_id: str,
        mode: str,
        *,
        registry: "CollectorRegistry | None" = None,
    ) -> None:
        from prometheus_client import REGISTRY, CollectorRegistry, Counter, Gauge

        self._experiment_id = experiment_id
        self._mode = mode
        self._registry: CollectorRegistry = registry or REGISTRY
        self._lock = threading.Lock()
        self._started = False

        lbl = ("experiment_id", "mode")
        static = dict(experiment_id=experiment_id, mode=mode)

        self._equity = Gauge(
            "bot_equity_usd",
            "Current RiskManager equity (USD).",
            lbl,
            registry=self._registry,
        )
        self._open_positions = Gauge(
            "bot_open_positions",
            "Count of open positions tracked by RiskManager.",
            lbl,
            registry=self._registry,
        )
        self._active_markets = Gauge(
            "bot_active_markets",
            "PolymarketFeed snapshot count (markets with at least one snapshot).",
            lbl,
            registry=self._registry,
        )
        self._kill_switch = Gauge(
            "bot_kill_switch_active",
            "1 if daily drawdown kill-switch is active, else 0.",
            lbl,
            registry=self._registry,
        )
        self._equity_drift_pct = Gauge(
            "bot_equity_drift_pct",
            "Last equity vs on-chain USDC drift (percent): 100 * (on_chain - bot_equity) / bot_equity.",
            lbl,
            registry=self._registry,
        )

        fill_lbl = ("experiment_id", "mode", "strategy", "side", "tif")
        self._fills = Counter(
            "bot_fills_total",
            "Fills recorded after reconciliation (notify_fill_metrics / live or sim).",
            fill_lbl,
            registry=self._registry,
        )
        notional_lbl = ("experiment_id", "mode", "strategy")
        self._fill_notional = Counter(
            "bot_fill_notional_usd_total",
            "Sum of |size * price| for fills (USD notional).",
            notional_lbl,
            registry=self._registry,
        )
        rej_lbl = ("experiment_id", "mode", "reason")
        self._rejections = Counter(
            "bot_order_rejections_total",
            "Live order rejections (CLOB).",
            rej_lbl,
            registry=self._registry,
        )
        disc_lbl = ("experiment_id", "mode", "feed")
        self._feed_disconnects = Counter(
            "bot_feed_disconnects_total",
            "Feed disconnects / reconnect cycles.",
            disc_lbl,
            registry=self._registry,
        )
        self._resolution_poll_failures = Counter(
            "bot_resolution_poll_failures_total",
            "Resolution / settlement poll failures (degraded path).",
            lbl,
            registry=self._registry,
        )
        unwind_lbl = ("experiment_id", "mode", "outcome")
        self._arb_unwinds = Counter(
            "bot_arb_unwinds_total",
            "Arb bundle outcomes (balanced / unwound / skipped).",
            unwind_lbl,
            registry=self._registry,
        )

        self._static = static
        self._equity.labels(**static).set(0.0)
        self._open_positions.labels(**static).set(0.0)
        self._active_markets.labels(**static).set(0.0)
        self._kill_switch.labels(**static).set(0.0)
        self._equity_drift_pct.labels(**static).set(0.0)

    def start(self, port: int) -> None:
        """
        Start the background /metrics HTTP server on 127.0.0.1 only (not all interfaces).
        """
        from prometheus_client import start_http_server

        with self._lock:
            if self._started:
                return
            start_http_server(port, addr="127.0.0.1", registry=self._registry)
            self._started = True

    def update_equity(self, value: float) -> None:
        self._equity.labels(**self._static).set(value)

    def update_open_positions(self, count: int) -> None:
        self._open_positions.labels(**self._static).set(count)

    def update_active_markets(self, count: int) -> None:
        self._active_markets.labels(**self._static).set(count)

    def set_kill_switch(self, active: bool) -> None:
        self._kill_switch.labels(**self._static).set(1.0 if active else 0.0)

    def update_drift(self, value: float) -> None:
        self._equity_drift_pct.labels(**self._static).set(value)

    def record_fill(
        self,
        strategy: str,
        side: str,
        mode: str,
        notional_usd: float,
    ) -> None:
        """
        Record a fill. The `mode` argument is the order time-in-force (e.g. IOC, GTC);
        it is exposed as Prometheus label `tif` to avoid clashing with the static `mode`
        label (sim/live).
        """
        self._fills.labels(
            experiment_id=self._experiment_id,
            mode=self._mode,
            strategy=strategy,
            side=side,
            tif=str(mode),
        ).inc()
        self._fill_notional.labels(
            experiment_id=self._experiment_id,
            mode=self._mode,
            strategy=strategy,
        ).inc(notional_usd)

    def record_rejection(self, reason: str) -> None:
        self._rejections.labels(
            experiment_id=self._experiment_id,
            mode=self._mode,
            reason=str(reason)[:200] or "unknown",
        ).inc()

    def record_feed_disconnect(self, feed: str) -> None:
        self._feed_disconnects.labels(
            experiment_id=self._experiment_id,
            mode=self._mode,
            feed=str(feed),
        ).inc()

    def record_resolution_poll_failure(self) -> None:
        self._resolution_poll_failures.labels(**self._static).inc()

    def record_arb_unwind(self, outcome: str) -> None:
        o = str(outcome)
        if o not in {"balanced", "unwound", "skipped"}:
            o = "skipped"
        self._arb_unwinds.labels(
            experiment_id=self._experiment_id,
            mode=self._mode,
            outcome=o,
        ).inc()
