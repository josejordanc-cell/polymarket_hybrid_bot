# Polymarket Hybrid Bot — Handoff (v0.2.9 dashboard-integrated)

Standalone onboarding document for developers and the dashboard team. No prior prompt history required.

---

## 1. Executive summary

The **Polymarket Hybrid Bot** is an async Python **3.11** service that trades Polymarket binary markets using a **strict strategy priority loop**: intra-market arbitrage first, then spot-lag momentum, then market making, with optional smart-money copy. It supports **simulation** (`PaperSimulator`) and **live** execution (`py-clob-client`), with **REST-based settlement polling**, **live fill reconciliation**, **arb partial-fill unwind**, **Prometheus `/metrics`**, **hourly on-chain USDC vs equity reconciliation**, and **JSON-log-driven weekly reports**. The v0.2.9 wave shipped the **dashboard wire-shape alignment** (canonical Zod source-of-truth) and **snapshot history wiring** for the `positions` / `fills` / `risk` / `equity` / `logs` / `alerts` slices; observability contracts (logs + metrics + HTTP/WS snapshot) are stable. The next step is a 48–72h sim validation on the DigitalOcean droplet, then a cautious live flip with small risk caps.

---

## 2. Architecture overview

| Layer | Module(s) | Role |
|-------|-----------|------|
| Entry | `main.py` | `run_bot`, feed tasks, resolution + equity loops, strategy priority, execution |
| Config | `config/settings.py` | `BotConfig` / `get_config()` — all `BOT_*` env vars |
| Data | `data_feeds/polymarket_feed.py`, `spot_feed.py`, `polygon_feed.py` | WS order book, spot returns (Binance + Coinbase via ccxt), optional Polygon wallet scan |
| Risk | `risk_manager.py` | Kelly sizing, fees, kill-switch, approvals, fills, settlement accounting |
| Execution | `execution/clob_executor.py` | Sim vs live CLOB; pending orders; fill reconciliation |
| Strategies | `strategies/intra_arbitrage.py`, `momentum_latency.py`, `market_maker.py`, `smart_money_copy.py` | Signals and order construction |
| Simulation | `simulator.py` | Paper fills, session summary |
| Observability | `utils/logging.py`, `utils/alerts.py`, **`observability/metrics_exporter.py`** | Structlog JSON, Telegram/Discord, Prometheus |
| Correlation helper | **`utils/fill_correlation.py`** | Per-fill return → `RiskManager.record_return` |
| On-chain reconcile | **`utils/equity_reconciliation.py`** | USDC balances, drift math, metrics/log scrape helpers |
| CLI tools | **`utils/wallet_reconcile_cli.py`**, **`scripts/wallet_reconcile.py`**, **`scripts/weekly_report.py`** | Ad-hoc drift check; weekly PnL report from logs |
| Deploy | `deploy/Dockerfile`, `deploy/docker-compose.yml`, **`deploy/DEPLOY_DO.md`** | Container, port **9091** for metrics |

Background asyncio tasks started from `main.py`: Polymarket WS feed, spot feed (Binance + Coinbase), Polygon feed, **`resolution_polling_loop`**, **`equity_reconciliation_loop`**.

---

## 3. Strategy descriptions (accuracy check)

1. **`intra_arbitrage`** — Enters when `YES_ask + NO_ask ≤ 1 - min_arb_edge` and liquidity gates pass. Emits two **IOC BUY** legs (YES + NO) with shared **`bundle_id`**. Live requires both token IDs. Partial fills trigger async **`reconcile_arb_bundle`** (unwind or skip with metrics).

2. **`momentum_latency`** — If no arb, uses spot log-return z-score (Binance / Coinbase via the SpotFeed) vs **`BOT_STRAT_MOMENTUM_Z_THRESHOLD`**; buys YES or NO with IOC. Gated by **`BOT_STRAT_MOMENTUM_ENABLED`**.

3. **`market_maker`** — If no arb/momentum, posts **GTC** bid/ask on YES with **`BOT_STRAT_MM_SPREAD_BPS`** and inventory skew. Notional in the main loop is **`bankroll_usd * 0.01`** (fixed config, not floating equity).

4. **`smart_money_copy`** — **Off by default**. Can **append** orders when enabled even if another strategy already produced intents.

---

## 4. Risk and execution layer (including new behaviors)

- **Approval:** `RiskManager.approve_orders` enforces kill-switch, position cap, per-trade cap, min notional; **intra_arbitrage bypasses correlation guard**; other strategies use `correlation_blocked`.
- **Fills:** `on_fill` updates equity, positions (internal `{market_id}_{outcome}` dict keys), logs **`fill_recorded`**, updates metrics. _Boundary note (v0.2.9 3d-fix):_ the dashboard wire form is the single collapsed string `{market_id}:{outcome}` (note the colon, not the underscore) produced by `api/routes_rest.py:_collapse_position_id`. Internal dict-key form and wire-side string id intentionally differ; the bot's internal storage stays as-is, and only the snapshot serializer applies the collapse.
- **Settlement:** `on_settlement` clears losing legs, credits winners, logs **`settlement`** per leg; **`market_resolved`** logged from `main.py` after poll applies settlement.
- **Live reconciliation:** Order submitted → `pending_orders[order_id]` → poll until terminal → `TradeResult` with actual `filled_size` / price.
- **Arb unwind:** IOC SELL on overfilled side capped by **`BOT_STRAT_ARB_MAX_UNWIND_BPS`**; outcomes **`arb_bundle_balanced`**, **`arb_unwind`**, **`arb_unwind_skipped`**, **`arb_unwind_fill_recorded`**.
- **On-chain reconciliation:** Hourly USDC (bridged +/or native) vs `risk.equity`; **`equity_reconciled`**, **`equity_drift_alert`** if beyond threshold.

---

## 5. Observability contract for the dashboard team

### 5a. Structlog event schema

All lines are JSON. **Global context** (when using `main.py`): `experiment_id`, `mode` (`sim`/`live`), plus `timestamp`, `level`, `event`.

Below: **`event` name** → **important fields** (non-exhaustive; extra fields may appear).

| Event | Key fields |
|-------|------------|
| `bot_starting` | `mode`, `version` |
| `bot_shutting_down` | `equity` |
| `feed_warmup_start` | `seconds` |
| `feed_warmup_complete` | — |
| `strategy_loop_started` | `markets`, `mode` |
| `signal_active` | `market`, `strategy`, `intents` |
| `trade_result` | `strategy`, `market`, `outcome`, `accepted`, `fill_price`, `size`, `reason` |
| `fill_recorded` | **(RiskManager)** `market`, `outcome`, `side`, `fill_price`, `size`, `fee_usd`, `equity` |
| `fill_recorded` | **(CLOB reconcile)** `order_id`, `market`, `strategy`, `observed_fill_price`, `observed_filled_size`, `submitted_price`, `submitted_size`, `terminal_reason` |
| `order_status_changed` | `order_id`, `market`, `reason`, `raw_status`, `tif`, `filled_size`, `submitted_size` |
| `live_order_accepted` | `market`, `outcome`, `side`, `price`, `size`, `order_id` |
| `live_order_rejected` | `market`, `status`, `error` |
| `live_order_missing_order_id` | `market`, `resp` |
| `live_order_exception` | `market`, `error` |
| `settlement` | `market`, `outcome`, `winning`, `pnl`, `equity` |
| `market_resolved` | `market_id`, `outcome`, `pnl_realised`, `equity_after` |
| `resolution_poll_failed` | `error`, `consecutive_failures` |
| `resolution_polling_degraded` | `error`, `consecutive_failures` |
| `equity_reconciliation_disabled` | `reason`, `error?` |
| `equity_reconciled` | `bot_equity`, `on_chain_usdc`, `drift_usd`, `drift_pct`, `usdc_source` |
| `usdc_both_bridged_and_native` | `bridged_usdc`, `native_usdc` |
| `equity_drift_alert` | `drift_pct`, `threshold_pct`, `drift_usd`, `on_chain_usdc`, `bot_equity` |
| `equity_reconciliation_failed` | `error`, `consecutive_failures` |
| `equity_reconciliation_degraded` | `error`, `consecutive_failures` |
| `arb_signal_detected` | `market_id`, `yes_ask`, `no_ask`, `implied_sum`, `edge` |
| `arb_skipped_missing_token_ids` | `market_id` |
| `arb_bundle_balanced` | `bundle_id`, `filled_yes`, `filled_no` |
| `arb_unwind` | `bundle_id`, `imbalance_size`, `unwind_side`, `expected_cost_bps`, `limit_price` |
| `arb_unwind_skipped` | `bundle_id`, `reason`, `unwind_side`, `imbalance_size`, … |
| `arb_unwind_fill_recorded` | `bundle_id`, `filled_size`, `fill_price` |
| `arb_reconcile_failed` | `bundle_id` (+ exception) |
| `arb_simulated` | `fills` |
| `momentum_signal` | `market_id`, `symbol`, `z_score`, `side`, `edge`, `confidence` |
| `smart_money_signal` | `market`, `wallet`, `win_rate`, `profit_factor`, `confidence` |
| `polymarket_ws_closed` | `reason`, `retry_in` |
| `polymarket_ws_subscribed` | `market_count` |
| `polymarket_feed_error` | `error`, `retry_in` |
| `spot_feed_connected` / `spot_feed_cancelled` | `venue`, `symbols` |
| `spot_feed_warmup_timeout` | `seconds`, `hint` |
| `polygon_feed_connected` / `polygon_feed_disabled` | `chain_id` / — |
| `kill_switch_triggered` | `drawdown_pct`, `limit`, `equity` |
| `new_trading_day` | `prev_pnl` |
| `position_cap_reached` | `limit` |
| `correlation_block` | `market`, `correlated_with`, `corr` |
| `market_cache_hit` | `count`, `age_min` |
| `market_cache_corrupt` | `error` |
| `market_discovery_api_call` | — |
| `market_discovery_failed` | `error` |
| `market_discovery_empty` | `hint` |
| `market_cache_written` | `count` |
| `market_cache_write_failed` | `error` |
| `no_markets_discovered` | `hint` |
| `session_summary` | (simulator summary keys) |
| `backtest_complete` | `rows` |
| `paper_fill` | (debug; simulator) |
| `telegram_alert_failed` / `discord_alert_failed` | `error` |

*Debug-level events* (`price_change_applied`, `kelly_size`, etc.) are omitted here for brevity; they follow the same JSON shape.

**`weekly_report.py` JSON (`--format json`) — top-level keys (not structlog events):**

Sourced from `scripts/weekly_report.py:79-102` (`WeeklyReport` dataclass; emitted via `asdict()` at `:104-106`). All 11 keys always present in the JSON output.

| Key | Type | Meaning |
|-----|------|---------|
| `week_start` | str (ISO date `YYYY-MM-DD`) | UTC start of the trailing-`--weeks N` window. |
| `week_end` | str (ISO date `YYYY-MM-DD`) | UTC end of the window (= report-generation time). |
| `trades_total` | int | Count of `trades.jsonl` rows whose `ts` falls inside the window. |
| `trades_filled` | int | In-window rows with `status` in (`filled`, `fill`, `filled_partial`, `partial_fill`); also rows with no `status` that look like executions (have `side` / `size` / `price` / `market_id` / `token_id`). |
| `trades_cancelled` | int | In-window rows with `status` in (`cancelled`, `canceled`, `cancel`). |
| `realized_pnl_usdc` | float | **Flat sum** over in-window rows of the first-present field among `realized_pnl_usdc` / `realized_pnl` / `pnl_usdc` / `pnl`. No grouping, no bundle matching. |
| `unrealized_pnl_usdc` | float | Flat sum over in-window rows of the first-present field among `unrealized_pnl_usdc` / `unrealized_pnl`. |
| `fees_paid_usdc` | float | Flat sum over in-window rows of the first-present field among `fees_paid_usdc` / `fee_usdc` / `fee` / `fees_usd`. |
| `sharpe` | number \| null | Annualised Sharpe ratio over the all-time `trades.jsonl` equity series (forward-filled daily; rf=0, sqrt(365)). `null` when fewer than 20 daily returns are available or volatility is zero. Added `9f951e5`. **Not windowed** — computed over the entire file, not the trailing `--weeks N` slice (the ≥20-day gate cannot be satisfied by a 7-day window). |
| `sortino` | number \| null | Annualised Sortino ratio over the same series (MAR=0, sqrt(365); downside deviation is std of negative returns only). `null` when fewer than 20 daily returns, zero downside returns, or degenerate downside std. Added `9f951e5`. **Not windowed** (same rationale as `sharpe`). |
| `notes` | list[str] | Human-readable explanatory notes — populated for "No trade log at ..." (file missing), "Trade log exists but no rows fell in the selected time window", or "Trade log is empty for this window". Empty list when the window produced rows. |

### 5b. Prometheus metric schema

**Static labels on all metrics below:** `experiment_id`, `mode` (`sim` or `live`).

| Metric | Type | Additional labels | Meaning |
|--------|------|-------------------|---------|
| `bot_equity_usd` | Gauge | — | RiskManager equity (USD) |
| `bot_open_positions` | Gauge | — | Open position count |
| `bot_active_markets` | Gauge | — | Snapshots with data in feed |
| `bot_kill_switch_active` | Gauge | — | 1 if daily kill-switch tripped |
| `bot_equity_drift_pct` | Gauge | — | `100 * (on_chain_usdc - bot_equity) / bot_equity` (last reconcile) |
| `bot_fills_total` | Counter | `strategy`, `side`, **`tif`** | Fills after reconciliation (**`tif`** = IOC/GTC/FOK — **not** `mode`; avoids duplicate label name with sim/live) |
| `bot_fill_notional_usd_total` | Counter | `strategy` | Sum of \|price × size\| |
| `bot_order_rejections_total` | Counter | `reason` | Live CLOB rejections |
| `bot_feed_disconnects_total` | Counter | `feed` | Disconnect/reconnect cycles |
| `bot_resolution_poll_failures_total` | Counter | — | Degraded resolution polling |
| `bot_arb_unwinds_total` | Counter | `outcome` ∈ `balanced`/`unwound`/`skipped` | Arb bundle outcome |

**Optionality:** Set `BOT_METRICS_ENABLED=false` — no HTTP server, `get_metrics_exporter()` returns `None`; `notify_fill_metrics` is a no-op.

### 5c. Sample outputs (parsers)

Placeholder values; **field names and exposition format** match production.

**Structlog (one JSON object per line):**

```json
{"timestamp":"2026-05-02T12:00:00.123456Z","level":"info","event":"bot_starting","experiment_id":"prelive_sim","mode":"sim","version":"v3"}
```

The same `event` value `fill_recorded` is emitted in two shapes: **RiskManager** (post-accounting: `fill_price`, `size`, `equity`) vs **live CLOB executor** (reconciliation: `observed_*`, `submitted_*`, `order_id`, `terminal_reason`) — parsers must branch on keys, not on `event` alone.

```json
{"timestamp":"2026-05-02T12:00:05.000000Z","level":"info","event":"fill_recorded","experiment_id":"prelive_sim","mode":"sim","market":"0xabc123def456","outcome":"YES","side":"BUY","fill_price":0.42,"size":25.0,"fee_usd":0.021,"equity":9987.5}
```

```json
{"timestamp":"2026-05-02T12:00:06.000000Z","level":"info","event":"fill_recorded","experiment_id":"prelive_sim","mode":"live","order_id":"0xord789","market":"0xabc123def456","strategy":"momentum_latency","observed_fill_price":0.418,"observed_filled_size":10.0,"submitted_price":0.42,"submitted_size":10.0,"terminal_reason":"ioc_partial_cancelled"}
```

```json
{"timestamp":"2026-05-02T14:30:00.000000Z","level":"info","event":"market_resolved","experiment_id":"prelive_sim","mode":"sim","market_id":"0xdeadbeef","outcome":"YES","pnl_realised":12.34,"equity_after":10012.34}
```

```json
{"timestamp":"2026-05-02T12:00:10.000000Z","level":"info","event":"arb_bundle_balanced","experiment_id":"prelive_sim","mode":"sim","bundle_id":"550e8400-e29b-41d4-a716-446655440000","filled_yes":100.0,"filled_no":100.0}
```

```json
{"timestamp":"2026-05-02T13:00:00.000000Z","level":"info","event":"equity_reconciled","experiment_id":"prelive_sim","mode":"sim","bot_equity":10000.0,"on_chain_usdc":10050.25,"drift_usd":50.25,"drift_pct":0.5025,"usdc_source":"native_usdc"}
```

**Prometheus text** (`curl -s http://127.0.0.1:9091/metrics` excerpt, ~18 lines):

```text
# HELP bot_equity_usd Current RiskManager equity (USD).
# TYPE bot_equity_usd gauge
bot_equity_usd{experiment_id="prelive_sim",mode="sim"} 1.000000e+04
# HELP bot_open_positions Count of open positions tracked by RiskManager.
# TYPE bot_open_positions gauge
bot_open_positions{experiment_id="prelive_sim",mode="sim"} 3.000000e+00
# HELP bot_active_markets PolymarketFeed snapshot count (markets with at least one snapshot).
# TYPE bot_active_markets gauge
bot_active_markets{experiment_id="prelive_sim",mode="sim"} 1.200000e+01
# HELP bot_equity_drift_pct Last equity vs on-chain USDC drift (percent)
# TYPE bot_equity_drift_pct gauge
bot_equity_drift_pct{experiment_id="prelive_sim",mode="sim"} 5.025000e-01
# HELP bot_fills_total Fills recorded after reconciliation
# TYPE bot_fills_total counter
bot_fills_total{experiment_id="prelive_sim",mode="sim",strategy="intra_arbitrage",side="BUY",tif="IOC"} 24.0
bot_fills_total{experiment_id="prelive_sim",mode="sim",strategy="market_maker",side="BUY",tif="GTC"} 156.0
# HELP bot_fill_notional_usd_total Sum of |size * price| for fills
# TYPE bot_fill_notional_usd_total counter
bot_fill_notional_usd_total{experiment_id="prelive_sim",mode="sim",strategy="intra_arbitrage"} 1.234500e+03
# HELP bot_resolution_poll_failures_total Resolution / settlement poll failures (degraded path).
# TYPE bot_resolution_poll_failures_total counter
bot_resolution_poll_failures_total{experiment_id="prelive_sim",mode="sim"} 0.0
```

Static labels on every series: **`experiment_id`**, **`mode`**. Fill counters add **`strategy`**, **`side`**, and **`tif`** (time-in-force — **not** sim/live).

### 5d. `wallet_reconcile.py` exit codes

| Code | Meaning |
|------|---------|
| **0** | Drift within threshold |
| **1** | Drift **breach** (\|drift\| > threshold) |
| **2** | Config / RPC / equity read failure (e.g. missing `BOT_POLYGON_RPC_URL`, bad wallet, `/metrics` and log scrape both failed) |

---

## 6. New tooling usage

### `/metrics` (port **9091**)

- **Scrape:** `http://<host>:9091/metrics` (container maps `9091:9091`).
- **Filter:** Use Prometheus `on()` / `* on(experiment_id, mode)` or Grafana label filters — every series carries **`experiment_id`** and **`mode`**.
- **Fill rates:** Query `bot_fills_total` broken out by **`tif`**, `strategy`, `side`.

### `scripts/weekly_report.py`

Trades-aggregate report driven by `BOT_DATA_DIR/trades.jsonl` (written per-fill by `utils.trades.persist_fill` since `d079f93`). Window is selected by `--weeks N` (default `1`); the file is the only input source — there are no `--logs` / `--start` / `--end` / `--experiment-id` flags. Always writes a file to `BOT_REPORT_DIR` (default `BOT_DATA_DIR/reports/weekly_report_<YYYYMMDD>.{md,json}`); `--stdout` is additive. Returns exit **0** even when the trade log is missing or empty (the report carries an explanatory note in that case).

```bash
# Trailing 1 week, markdown, file only (canonical production invocation per DEPLOY_DO.md:37)
python -m scripts.weekly_report --weeks 1

# Trailing 4 weeks, JSON, also print to stdout
python -m scripts.weekly_report --weeks 4 --format json --stdout

# Verbose: print BOT_DATA_DIR + output path on stderr
python -m scripts.weekly_report --weeks 1 -v
```

**PnL model (v0.2.1+ — `554e2a8`):** The report computes **flat-aggregate** PnL by summing per-row `realized_pnl_usdc` / `unrealized_pnl_usdc` / `fees_paid_usdc` across in-window `trades.jsonl` rows (`scripts/weekly_report.py:191-216`). There is **no per-bundle FIFO leg-pairing** and **no `bundle_id` matching** in the weekly report; the v0.2.0 log-driven arb-bundle PnL model was retired in `554e2a8` (2026-05-02) along with the report rewrite from structlog logs to `BOT_DATA_DIR/trades.jsonl`.

> **Per-bundle arb telemetry still exists at runtime — just not in the weekly report.** The bot continues to emit structlog events `arb_bundle_balanced` / `arb_unwind` / `arb_unwind_skipped` with `bundle_id` (`strategies/intra_arbitrage.py:63,87,102,118,167`) and the Prometheus counter `bot_arb_unwinds_total{outcome}` (`observability/metrics_exporter.py:138`). Grep structlog or query Prometheus for bundle-level audit; the offline aggregator was removed but the data stream is intact.

### `scripts/wallet_reconcile.py`

```bash
# Default: equity from http://127.0.0.1:BOT_METRICS_PORT/metrics
python scripts/wallet_reconcile.py

python scripts/wallet_reconcile.py --threshold 2.5 --metrics-host 127.0.0.1

# Skip HTTP; scrape latest equity from JSON log
python scripts/wallet_reconcile.py --no-metrics --log-file /var/log/bot.jsonl

python scripts/wallet_reconcile.py --json

# Optional alert (Telegram/Discord if configured)
python scripts/wallet_reconcile.py --alert --threshold 1.0
```

Uses **`BOT_POLYGON_RPC_URL`**, **`BOT_POLYGON_WALLET_ADDRESS`** or private key derivation, **`BOT_RECONCILIATION_DRIFT_THRESHOLD_PCT`**, **`BOT_METRICS_PORT`**, optional **`BOT_METRICS_HOST`**, **`BOT_LOG_PATH`**.

---

## 7. Configuration reference (`BOT_*` from v2.1 work)

Grouped by area (see `deploy/.env.example` for full template).

| Variable | Default | Description |
|----------|---------|-------------|
| **Experiment / metrics** | | |
| `BOT_EXPERIMENT_ID` | `default` | A/B arm id; bound to logs + metrics |
| `BOT_METRICS_ENABLED` | `true` | Expose Prometheus HTTP server |
| `BOT_METRICS_PORT` | `9091` | `/metrics` port |
| **Settlement** | | |
| `BOT_RESOLUTION_POLL_SECONDS` | `60` | Gamma resolution poll interval |
| **Live fills** | | |
| `BOT_FILL_POLL_INTERVAL_SEC` | `2` | REST poll interval for order status |
| `BOT_FILL_POLL_TIMEOUT_SEC` | `60` | Max wait for terminal state |
| **On-chain reconcile** | | |
| `BOT_RECONCILIATION_INTERVAL_SECONDS` | `3600` | Equity vs chain period |
| `BOT_RECONCILIATION_DRIFT_THRESHOLD_PCT` | `1.0` | Alert threshold (\|%\|) |
| `BOT_POLYGON_WALLET_ADDRESS` | *(empty)* | Override checksummed address |
| `BOT_POLYGON_RPC_URL` | *(empty)* | Required for reconciliation + wallet CLI |
| **CLI helpers (no pydantic field)** | | |
| `BOT_METRICS_HOST` | `127.0.0.1` | Metrics scrape host for wallet CLI |
| `BOT_LOG_PATH` | `bot.log` | Log path for wallet CLI fallback |

All other `BOT_*` / `BOT_RISK_*` / `BOT_STRAT_*` vars are unchanged from prior releases (bankroll, Kelly, drawdown, strategy toggles, API keys, etc.).

---

## 8. Operational runbook

### Deploy / redeploy (DigitalOcean)

1. SSH to droplet; use non-root deploy user with Docker (`deploy/DEPLOY_DO.md`).
2. Pull repo; ensure `.env` at repo root (from `deploy/.env.example`).
3. From `deploy/`: `docker compose build && docker compose up -d`.
4. Confirm: `docker ps`, `docker logs -f polymarket-hybrid-bot`, `curl -s localhost:9091/metrics | head`.

### Sim vs live

- **Sim:** `command: ["python", "main.py", "--sim"]` in compose (default) or run locally: `python main.py --sim`.
- **Live:** Set credentials in `.env`, `command: ["python", "main.py", "--live"]`. CLI sets `BOT_MODE` before config load — keep `.env` `BOT_MODE` consistent or rely on CLI.

### Manual reconciliation

```bash
python scripts/wallet_reconcile.py --json
```
Exit **0** = within threshold; **1** = breach; **2** = error.

### Alerts (Telegram / Discord)

| Trigger | Level | Typical meaning |
|---------|-------|-----------------|
| Bot start / shutdown | info / warning | Process lifecycle |
| `kill_switch_triggered` | critical | Daily drawdown limit |
| `resolution_polling_degraded` | critical | Settlement poll failures ≥5 |
| `equity_reconciliation_degraded` | critical | RPC failures ≥5 |
| `equity_drift_alert` | warning (via `maybe_alert_equity_drift`) | On-chain vs bot equity |
| Large fill (`risk_manager`) | info | Notional ≥ 5% of bankroll |
| Settlement / PnL alert | info / warning | Per-leg settlement |
| Healthcheck (optional cron) | varies | See `healthcheck.py` |

---

## 9. Monitoring, testing, validation checklist

- **Unit/integration tests:** `pytest` (see `TESTING_CHECKLIST.md`).
- **Production monitoring:** Docker logs, `/metrics`, optional Loki/Grafana; drift + resolution alerts.
- **Multi-day sim:** See **`docs/VALIDATION_CHECKLIST.md`**.

---

## 10. Known limitations (ops / dashboard framing)

- Correlation guard's pairing keying bug closed 2026-05-08 (T5, PR #28). Guard now actively evaluates; expect `correlation_block` log events in sim. Threshold (`cfg.risk.max_pair_correlation`, default `0.80`) is a tuning concern before live flip.
- **pUSD / collateral** not in USDC RPC balance → drift alerts can fire during normal Polymarket flows; treat drift as **directional signal**, not absolute truth.
- **Python 3.10+** required (`slots` on dataclasses); Docker uses **3.11**.
- **`pending_orders`**: lost on restart; possible duplicate or stuck order handling needs ops playbooks.
- **Weekly drawdown / Sharpe / Sortino** in `weekly_report` derive from `trades.jsonl` `equity_after` samples (written per-fill by `utils.trades.persist_fill` since `d079f93`) — Sharpe/Sortino consume a forward-filled daily series via `utils.derived_stats.build_daily_equity_series_from_trades` (since `9f951e5`). Still not a full mark-to-market equity curve: gap days carry the prior fill's equity (0.0 return on gap days), and intra-day mark-to-market is not captured.

---

## 11. Roadmap

- Trade ledger DB; Polygon settlement listener; pUSD in reconcile; persistent pending orders. See `docs/FINAL_CODE_REVIEW.md` P3 backlog. (Sharpe/Sortino in weekly report DONE 2026-05-26 at `9f951e5` — see `CHANGELOG.md` [Unreleased] "Weekly report — Sharpe + Sortino".)

---

## 12. Live-flip and dashboard integration

**Dashboard:** Scrape **`/metrics`** from port **9091**; join on **`experiment_id`** and **`mode`**. Ship JSON logs to Loki/ELK for event-level dashboards; use §5a event names.

**Live flip (high level):** Complete **`docs/VALIDATION_CHECKLIST.md`** on sim; confirm Grafana/dashboard reads metrics; set **`BOT_RISK_MAX_TRADE_PCT`** and **`BOT_RISK_KELLY_FRACTION`** small for first live week; switch compose to `--live`; monitor `live_order_rejected`, `equity_drift_alert`, `arb_unwind_skipped`.

---

## Related documents

- `docs/FINAL_CODE_REVIEW.md` — engineering review + known bugs
- `docs/VALIDATION_CHECKLIST.md` — 48–72h sim pass/fail criteria
- `docs/NEXT_STEPS.md` — dated priorities
- `TESTING_CHECKLIST.md` — quick verification commands
- `deploy/DEPLOY_DO.md` — droplet setup
