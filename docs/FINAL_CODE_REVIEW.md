# Final code review — v2.1 pre-dashboard (post nine-prompt milestone)

Senior-engineer review of the Polymarket hybrid bot after Prompts 1–9. This document complements `HANDOFF.md` for auditors and onboarding developers.

---

## 1. Settlement wiring (Prompt 1)

**What works well:** `resolution_polling_loop` in `main.py` polls Gamma REST via `poll_market_resolutions` only when there are open positions, applies `RiskManager.on_settlement`, then emits structured `market_resolved` with `pnl_realised` and `equity_after`. Failures log `resolution_poll_failed`; after five consecutive failures, `resolution_polling_degraded` fires, Telegram/Discord alert (critical), and `bot_resolution_poll_failures_total` increments. Backoff is exponential capped at 600s.

**What is fragile:** Resolution IDs must match Gamma `conditionId` casing; polling is REST-bound (~`BOT_RESOLUTION_POLL_SECONDS` minimum practical latency). If Gamma returns partial data for batched `condition_ids`, some resolutions could be missed until the next cycle. `on_settlement` logs `settlement` per outcome leg; `market_resolved` is one line per resolved market from the poll loop — both are useful but readers must not double-count PnL.

**Technically correct but operationally questionable:** Lowering `BOT_RESOLUTION_POLL_SECONDS` aggressively will rate-limit before materially improving time-to-settlement; sub-minute needs an on-chain listener (P3).

---

## 2. Live fill reconciliation (Prompt 2)

**What works well:** Live path uses `pending_orders` keyed by `order_id`, REST polling (`BOT_FILL_POLL_INTERVAL_SEC` / `BOT_FILL_POLL_TIMEOUT_SEC`) with terminal-state logic for partial IOC fills (`ioc_partial_cancelled`). Reconciliation logs `order_status_changed` and a second `fill_recorded` variant with `observed_fill_price` / `submitted_price` for audit. Sim path never touches `pending_orders`; execution goes straight to `PaperSimulator`.

**What is fragile:** In-memory `pending_orders` only; restart mid-order loses tracking. No user fill WebSocket in SDK path — REST polling only.

**Technically correct but operationally questionable:** High fill volume increases RPC load and log volume; tune intervals per account tier.

---

## 3. Arb partial-fill compensation (Prompt 3)

**What works well:** `bundle_id` ties YES/NO legs; after both legs execute, `reconcile_arb_bundle` runs in a fire-and-forget `asyncio.create_task`. Balanced fills log `arb_bundle_balanced` and metric `balanced`. Imbalance triggers IOC SELL unwind or `arb_unwind_skipped` with explicit reason; `bot_arb_unwinds_total{outcome=...}` records one outcome per bundle.

**What is fragile:** Unwind uses snapshot bid at schedule time; fast markets can gap. Exceptions in reconcile log `arb_reconcile_failed` with stack — worth alerting on in production.

**Technically correct but operationally questionable:** Skipped unwinds leave directional risk; ops must watch `arb_unwind_skipped` and positions.

---

## 4. Prometheus metrics exporter (Prompt 8)

**What works well:** `MetricsExporter` registers gauges/counters with static labels `experiment_id` and `mode` (sim/live). Optional: `BOT_METRICS_ENABLED=false` skips HTTP server and sets exporter to `None`. Fill counters use label **`tif`** (IOC/GTC/FOK) for time-in-force — not reusing the name `mode` — avoiding duplicate label keys in Prometheus.

**What is fragile:** High-cardinality `reason` on `bot_order_rejections_total` is truncated to 200 chars but can still grow if many unique errors.

**Technically correct but operationally questionable:** `bot_active_markets` reflects snapshot count in the strategy loop, not static discovery count — correct for “markets with data,” confusing if someone expects “subscribed tokens.”

---

## 5. experiment_id binding (Prompt 4)

**What works well:** `structlog.contextvars.bind_contextvars(experiment_id=..., mode=...)` in `run_bot` before logging; JSON logs carry both on every event.

**What is fragile:** `get_config()` is LRU-cached; `BOT_MODE` must be set before first `get_config()` call (CLI does this). Changing experiment without process restart may not refresh cached config in odd embedding scenarios.

**Technically correct but operationally questionable:** Logs without `experiment_id` (legacy) are invisible to `weekly_report.py` unless you pass a nonexistent id (report empty) — strict by design.

---

## 6. record_return wiring (Prompt 5)

**What works well:** `record_fill_return_for_correlation` computes directional return vs `signal_reference_price` (or snapshot mid) and calls `risk.record_return(market_id, ret)` after each reconciled fill in `main.py` and after arb unwind fills.

**What is fragile:** *(Closed 2026-05-08 by T5 — PR #28, commit `704ddcc`.)* The original `correlation_blocked` implementation iterated `open_positions` composite keys (`{market_id}_{outcome}`) and looked them up in `_return_history` (keyed by bare `market_id`); every lookup returned `[]` so the guard was a silent no-op. The fix iterates `self._state.open_positions.values()` and reads `Position.market_id` (option c — matches the existing pattern at `main.py:99,110`). Guard is now actively evaluating.

**Technically correct but operationally questionable:** Arb bypasses the correlation guard by design in `approve_orders`; non-arb strategies now correctly evaluate post-T5. Threshold tuning (`cfg.risk.max_pair_correlation`, default `0.80`) is a separate concern before the live flip.

---

## 7. Config consolidation (Prompt 6)

**What works well:** Single `config/settings.py` / `BotConfig`; `config/config.py` removed from tree. `deploy/.env.example` lists new variables.

**What is fragile:** Third-party docs or old branches may still reference removed paths.

**Technically correct but operationally questionable:** None significant.

---

## 8. Test harness (Prompt 4.5)

**What works well:** `pytest` + `pytest-asyncio`; `conftest.py` stubs `py_clob_client` when missing, forces `BOT_MODE=sim` and `BOT_EXPERIMENT_ID=test`. Targeted tests: resolution polling, live fill reconciliation, arb bundle, metrics exporter, equity reconciliation, weekly report, correlation/record_return, smoke.

**What is fragile:** Stub `ClobClient` is minimal; integration gaps remain for real SDK responses.

**Technically correct but operationally questionable:** CI should pin Python 3.11 to match Docker.

---

## 9. On-chain equity reconciliation (Prompt 9)

**What works well:** Dual USDC: bridged (USDC.e) and native Circle USDC; `resolve_polygon_usdc_human` sums if both non-zero with `usdc_both_bridged_and_native` warning. Drift: `compute_drift_pct`; gauge `bot_equity_drift_pct`; loop logs `equity_reconciled` hourly (configurable). RPC errors backoff and escalate to `equity_reconciliation_degraded` + alert after 5 failures. CLI `scripts/wallet_reconcile.py` can scrape `/metrics` or log file.

**What is fragile:** **pUSD / Polymarket collateral not in ERC-20 USDC balance** — wallet can show “wrong” drift vs internal equity when collateral is wrapped or held in contracts.

**Technically correct but operationally questionable:** Comparing full wallet USDC to bot equity ignores open conditional token mark-to-market — drift is a sanity check, not a full balance sheet.

---

## 10. Weekly performance report (Prompt 7)

**What works well:** Log-only; **strict `experiment_id`**: omitting `--experiment-id` lists candidates and exits 2. Arb PnL: FIFO pairing of YES/NO `trade_result` lines per market prefix, then per-bundle `leg_yes_pnl + leg_no_pnl + unwind_pnl`; `arb_unwind_skipped` tracked as notes, **not** in bundle PnL. Drawdown: peak-to-trough on `fill_recorded.equity` series only.

**What is fragile:** Orphan arb legs and unmatched completion queues surface as warnings in report JSON. Drawdown ignores equity before first `fill_recorded` in window.

**Technically correct but operationally questionable:** Market maker “horizon equity delta” is end-start equity, not inventory-adjusted MTM — documented in report output as simplified.

---

## KNOWN UNFIXED ITEMS

| Item | Severity | Suggested follow-up |
|------|----------|---------------------|
| pUSD / non-USDC collateral not in `wallet_reconcile` | **Medium** | Document; add pUSD or contract balance in P3. **Timeline: before using drift alerts as hard stops.** |
| Python 3.9 vs 3.11: `@dataclass(slots=True)` in `risk_manager` requires 3.10+ | **Medium** | Enforce 3.11 in CI/Docker only (already in Dockerfile); add `requires-python` in packaging if publishing. **Timeline: immediate doc/CI.** |
| `backtest.py` calls `notify_fill_metrics` | **Low** | Currently no-op unless an exporter is registered; if backtests register Prometheus, use distinct `experiment_id`/`mode` labels (e.g. `mode=sim` + `experiment_id=backtest`). **Timeline: when wiring metrics to backtests.** |
| `pending_orders` in-memory only | **Medium** | Accept for v1; persistence P3. **Timeline: post-live if restarts prove painful.** |

---

## P3 BACKLOG (deferred)

- Trade ledger DB (replace JSON scraping for weekly report).
- Sharpe / Sortino in weekly report.
- Polygon contract listener for sub-minute settlement.
- pUSD balance integration in `wallet_reconcile`.
- `pending_orders` persistence.

---

## ADDITIONAL EDGE CASES (this review)

- **Duplicate event name `fill_recorded`:** RiskManager emits canonical fill + equity; `clob_executor` emits reconciliation detail with `observed_*` fields. Weekly report keys off `fill_price`/`size` on `fill_recorded` — executor line uses `observed_fill_price`, so it does not double-enter the fill backlog (by design).
- **`settlement` vs `market_resolved`:** Both occur around resolution; weekly report counts `market_resolved` incidents, not `settlement` leg lines.
- **Gamma API batching:** Very large open-position sets could hit URL/param limits — monitor `resolution_poll_failed`.
- **Wallet reconcile exit 1:** Drift breach is non-zero exit for automation — ensure cron distinguishes 1 vs 2.
- **parse_bot_equity_from_metrics:** If multiple `experiment_id` series exist in one scrape, matcher prefers exact label match; fallback “best” value could misread if multiple bots share a port (unlikely).
