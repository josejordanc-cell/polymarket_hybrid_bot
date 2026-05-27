# Pre–live-flip validation checklist (48–72h sim on DigitalOcean)

Use this during the **simulation** run before switching compose to `--live`. Each item is a **pass/fail** signal with **how to check** and **pass criterion**.

Assume JSON logs are collected (Docker `json-file` or shipper), `BOT_EXPERIMENT_ID` is set (e.g. `prelive_sim_may2026`), and `BOT_METRICS_ENABLED=true`, `BOT_METRICS_PORT=9091`.

---

## Runtime and identity

### 1. `bot_starting` once per restart with correct `experiment_id`

- **Check:** Search logs for `"event": "bot_starting"` after each deploy/restart; confirm same line includes `experiment_id` from structlog context.
- **How:** `docker logs polymarket-hybrid-bot 2>&1 | grep bot_starting` or Loki query `{container="polymarket-hybrid-bot"} |= "bot_starting"`.
- **Pass:** Exactly one `bot_starting` per process start; `experiment_id` matches `.env` `BOT_EXPERIMENT_ID`.

### 2. `/metrics` reachable on 9091 for full run

- **Check:** HTTP GET to metrics port from host or sidecar.
- **How:** Cron every 5m: `curl -sf -o /dev/null -w '%{http_code}' http://127.0.0.1:9091/metrics` → expect `200`.
- **Pass:** No sustained gaps > 15m (allow brief restarts).

### 3. `experiment_id` on every metric and log line

- **Check:** Sample metrics and log lines.
- **How:** `curl -s http://127.0.0.1:9091/metrics | grep experiment_id` — every exported series should repeat the same id. For logs, `grep -v experiment_id bot.log | head` should show **no** complete lines missing the field (structlog context).
- **Pass:** 100% of sampled lines carry `experiment_id`; Prometheus labels match.

---

## Business logic signals

### 4. `market_resolved` with non-zero `pnl_realised` / `equity_after`

- **Check:** Markets that resolve during the window.
- **How:** Logs: `"event": "market_resolved"` with numeric `pnl_realised`, `equity_after`.
- **Pass:** At least one such event if any held position resolved; values consistent with sequential equity (no NaN, no null).

### 5. `fill_recorded` shows observed vs submitted (live fill reconciliation)

- **Sim mode:** Does **not** exercise `pending_orders` or executor `observed_fill_price` / `submitted_price` reconciliation — **this item is N/A during sim** and **must be re-validated after live flip**.
- **Live mode:** Find `fill_recorded` lines (executor shape) with `observed_fill_price` vs `submitted_price` and/or `observed_filled_size` vs `submitted_size` when IOC partials occur; at least one meaningful deviation **or** documented zero partials with engineer sign-off.

### 6. Arb bundle outcomes never silent

- **Check:** Every arb bundle produces one of `arb_bundle_balanced`, `arb_unwind`, `arb_unwind_skipped`.
- **How:** Count arb `trade_result` pairs vs completion events; Prometheus `sum(increase(bot_arb_unwinds_total{experiment_id="..."}[72h]))` > 0 if arbs ran.
- **Pass:** No “orphan” arb in weekly report warning without investigation; if `intra_arbitrage` traded, at least one arb outcome metric/event.

### 7. `equity_reconciled` roughly hourly

- **Check:** Reconciliation loop cadence.
- **How:** Log grep `equity_reconciled`; intervals ≈ `BOT_RECONCILIATION_INTERVAL_SECONDS` (± RPC jitter).
- **Pass:** ≥ `floor(72h / interval) - 1` events; no 6h+ gap without `equity_reconciliation_failed` explanation.

### 8. `bot_equity_drift_pct` populated

- **How:** `curl -s :9091/metrics | grep bot_equity_drift_pct`.
- **Pass:** Gauge line present; value is finite (not stuck only at 0 if chain and bot disagree — investigate large constant zero).

### 9. No sustained `resolution_polling_degraded`

- **How:** Logs for `resolution_polling_degraded`.
- **Pass:** None; **or** if present, followed by successful `market_resolved` / cleared failures within 2 poll cycles and root cause noted.

### 10. No sustained `equity_reconciliation_degraded`

- **How:** Same pattern for equity loop.
- **Pass:** None; **or** transient RPC blips with recovery and optional RPC upgrade.

### 11. No unhandled exception traces

- **How:** `docker logs ... 2>&1 | grep -iE 'traceback|exception|ERROR'` — review each hit.
- **Pass:** No uncaught stack traces from `main` loop; expected warnings (`arb_unwind_skipped`, etc.) are OK.

### 12. Memory and CPU stable

- **How:** `docker stats polymarket-hybrid-bot --no-stream` at start, +24h, +48h (note `MEM USAGE` / `%CPU`).
- **Pass (steady-state expectations):** RSS **200–500 MB** typical; CPU **5–20%** average with **spikes** during market discovery / cache refresh.
- **Fail:** RSS **grows monotonically** over **24h** (leak suspicion). **Fail:** CPU **sustains above 80%** (excluding brief spikes & restarts).

### 13. Polymarket WS reconnects cleanly

- **How:** Logs: `polymarket_ws_closed` then later `polymarket_ws_subscribed` or steady order book updates.
- **Pass:** After `polymarket_ws_closed`, feed recovers without manual restart; no tight crash loop.

---

## Pre-flip readiness (end of sim)

### 14. `weekly_report.py` on the sim window

```bash
# Sim runs for 48–72h, so --weeks 1 covers the window; bump if needed.
python -m scripts.weekly_report --weeks 1 --format json --stdout
```

- **Pass:** Exit 0; JSON well-formed; `trades_filled > 0` (the writer at `utils.trades.persist_fill` populates `BOT_DATA_DIR/trades.jsonl` on every accepted fill since `d079f93`); `fees_paid_usdc` non-negative and consistent with `BOT_RISK_BANKROLL_USD`. If `notes` carries a "No trade log at ..." or "Trade log is empty" entry, the sim did not produce any fills — investigate before flipping.

> **Note (post-CLI-modernisation):** the legacy `arb_unmatched_pairs` / `arb_unmatched_completions` top-level keys are NOT emitted by the current trades-aggregate report (current `WeeklyReport` fields: `trades_total`, `trades_filled`, `trades_cancelled`, `realized_pnl_usdc`, `unrealized_pnl_usdc`, `fees_paid_usdc`, `sharpe`, `sortino`, `notes`). Arb-bundle FIFO accounting was a property of the log-driven v0.2.0 report; the JSONL-driven v0.2.1+ report does not reconstruct bundles. If you need bundle-level audit, parse structlog `arb_bundle_balanced` / `arb_unwind` / `arb_unwind_skipped` events directly.

### 15. Manual `wallet_reconcile.py`

- **Pass:** Exit 0 or 1 with sensible JSON; not 2.

### 16. Dashboard reads `/metrics`

- **Pass:** Grafana (or chosen stack) shows `bot_equity_usd` and `bot_equity_drift_pct` with correct labels.

### 17. Live week risk caps

- **Check:** `.env` for first live week.
- **Pass:** `BOT_RISK_MAX_TRADE_PCT` and `BOT_RISK_KELLY_FRACTION` set to **small** values per risk committee (e.g. well below defaults).

---

**Overall go/no-go:** All **must-pass** items green or waived in writing. Block live flip on any open `equity_reconciliation_degraded` / `resolution_polling_degraded` without mitigation.
