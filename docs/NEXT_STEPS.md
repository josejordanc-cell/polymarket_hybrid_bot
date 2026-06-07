# Next steps (prioritized)

Rough effort and dependencies. Dates relative to **2026-05-02**.

> **Status orientation (2026-05-21, updated 2026-05-26):** v0.2.0 shipped,
> and the v0.2.9 wave shipped the dashboard wire-shape alignment + snapshot
> history wiring (`positions` / `fills` / `risk` / `equity` / `logs` /
> `alerts`). The 2026-05-26 profitability epic shipped pieces (1)
> `utils/trades.persist_fill` and (2a) `utils/fills.append_state_fill_record`
> in commit `d079f93`. Piece (2b) — equity/positions BotState mirror —
> landed at commit `8ff2236` (turned out to be fence-safe via the
> public-accessor polling-loop variant) and was runtime-verified live on
> the droplet at commit `54cf1fc`. The follow-on derived-stats epic
> closed the last five `GlobalStats` zero stubs: PR1 (`e92f915`) wired
> `pnlMonth` / `pnlAllTime` / `totalTrades`; PR2 (`9e26fae`) wired
> `winRate` (0–100 percentage) and `streak` (signed) via a new
> settlement-persistence pipeline (`utils/settlements.py`) and made the
> suite hermetic at 464 passing. **GlobalStats epic COMPLETE.** Droplet
> redeployed to `57d8b18` (image `sha256:89676eb4`). The two formerly-PASSIVE
> owed-gaps are now **CLOSED this session** — on-demand `/debug/trigger-fill`
> (`4457c97`) + `/debug/trigger-settlement` (`00dc0e5`) hatches populate
> `trades.jsonl` / `settlements.jsonl` deterministically, and the
> settlement-pnl round-trip fix (`e11f210`) + strategy-merge donut fix
> (`7255010`) shipped; live settlement smoke verified `winRate` 0→100→50.
> Items marked **DONE** below are already in `main`; the
> live priorities are the still-open items + anything new captured under
> `CHANGELOG.md` `[Unreleased]`. The §4 "Dashboard integration" block is
> superseded by the v0.2.9 wave — see CHANGELOG and `.swarm-decisions.md`
> (entries prefixed `v029-`, `b1-`, `2b-`, `derived-stats-pr1`,
> `derived-stats-pr2`) for details.

---

## 1. Immediate (today)

| Action | Effort | Notes |
|--------|--------|--------|
| Merge any outstanding PRs to `main` | **Small** (hours) | Single maintainer workflow |
| Tag / release **v0.2.0** after doc merge | **Small** | **DONE** — see CHANGELOG `[0.2.0]` row |
| Kick off **48–72h sim** on DO droplet with fixed `BOT_EXPERIMENT_ID` | **Small** ops | Use `docs/VALIDATION_CHECKLIST.md` |

---

## 2. During the sim run

| Action | Effort | Notes |
|--------|--------|--------|
| Share dashboard stack details with dashboard team | **Small** meeting | **DONE** — v0.2.9 wave reconciled the bot ↔ dashboard wire shape against the canonical Zod source (see CHANGELOG `[Unreleased]` v0.2.9 + `.swarm-decisions.md` `v029-*` rows). `docs/HANDOFF.md` §5 still valid for the structlog event names + Prometheus schema. |
| Expose `/metrics` to scraper (localhost agent or firewall allowlist) | **Small** | See `deploy/DEPLOY_DO.md` §1 |
| Confirm log pipeline retains `experiment_id` field | **Small** | Required for weekly report and Loki dashboards |

---

## 3. Post-sim (before live)

| Action | Effort | Notes |
|--------|--------|--------|
| Walk `docs/VALIDATION_CHECKLIST.md`; file waivers | **Medium** | Engineering + risk sign-off |
| Run `weekly_report.py` on full window; archive JSON | **Small** | |
| Fix **correlation guard** keying if you intend to rely on it | **1 PR** (~0.5–1 day) | **DONE** 2026-05-08 — T5 (PR #28, commit `704ddcc`); guard now actively evaluates. Threshold tuning (`cfg.risk.max_pair_correlation`, default `0.80`) still pending before live flip. |

---

## 4. Dashboard integration

**Superseded by the v0.2.9 wave** (see CHANGELOG `[Unreleased]` + `.swarm-decisions.md`
`v029-*` rows). The bot now exposes `GET /dashboard/snapshot` + `GET /ws` (FILL /
ALERT / TICK broadcast) per the canonical Zod contract; metrics scraping below
remains accurate.

| Action | Effort | Notes |
|--------|--------|--------|
| Import Prometheus scrape target `host:9091` | **Small** | No bot code change if Prometheus-native |
| Build Loki/Grafana panels from §5a events | **Medium** | Depends on stack |
| Bot-side changes | **Usually none** | Only if auth proxy or custom `/metrics` path requested |

**Dependency:** Scraper network path to 9091.

---

## 5. Live flip

| Criteria | |
|----------|--|
| `VALIDATION_CHECKLIST` passes | |
| Dashboard shows equity + drift | |
| `BOT_RISK_MAX_TRADE_PCT` / `BOT_RISK_KELLY_FRACTION` reduced for week 1 | |
| Runbook: rollback = compose back to `--sim` | |

**Effort:** **Small** ops event; **not** a refactor.

---

## 6. P3 backlog ordering

| Item | Effort | Why first / later |
|------|--------|-------------------|
| **Correlation guard fix** | 1 PR | **DONE** 2026-05-08 — T5, PR #28, commit `704ddcc` (see §3 above) |
| **Trade ledger DB** | Multi-day | **Partial** — JSONL ledger landed d079f93 (`utils/trades.persist_fill` writes `BOT_DATA_DIR/trades.jsonl` per accepted fill); full DB / indexed store remains future work for high-volume aggregation |
| **pending_orders persistence** | Medium | If restarts during live cause pain |
| **Polygon settlement listener** | Multi-day | Latency-sensitive strategies only |
| **pUSD in wallet_reconcile** | Medium | Reduces false drift alerts |
| **Sharpe/Sortino in weekly report** | Small PR | **DONE** 2026-05-26 — commit `9f951e5` (decision-log row `e217907`, `.swarm-decisions.md` `sharpe-sortino-weekly-report`). `compute_sortino` added to `utils/risk_metrics.py` alongside `compute_sharpe` (mirrors locks 3–5: rf=MAR=0, sqrt(365), ≥20 returns); `build_daily_equity_series_from_trades` + `_parse_trade_ts` added to `utils/derived_stats.py` (last `equity_after` per UTC day, forward-fill flat across gap days); `WeeklyReport.sharpe`/`.sortino` fields + markdown rows in `scripts/weekly_report.py`. Locked all-time scope (not windowed — a 7-day window cannot satisfy the ≥20-return gate), "n/a (insufficient data)" markdown placeholder. Suite 447 → 464 (+17 tests). Fence-safe (`risk_manager.py` untouched). Honest post-mortem on the "natural home" prediction below: half-right and half-wrong. The series-builder DID land in `utils/derived_stats.py` (reusing PR1's `_iter_jsonl_records` + `_resolve_trades_path` — correct prediction). Sortino did NOT land in `utils/settlements.py`; it landed in `utils/risk_metrics.py` alongside `compute_sharpe`, which is the actual canonical home for risk-ratio helpers (a fact this row missed at write time). `utils/settlements.py` was untouched. |

Honest sizing: **ledger DB + listener** are **multi-day refactors**; correlation fix and Sharpe **were** **single PR** scale (correlation fix DONE 2026-05-08 at `704ddcc`; Sharpe/Sortino in weekly report DONE 2026-05-26 at `9f951e5`).
