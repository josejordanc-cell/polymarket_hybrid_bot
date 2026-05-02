# Next steps (prioritized)

Rough effort and dependencies. Dates relative to **2026-05-02**.

---

## 1. Immediate (today)

| Action | Effort | Notes |
|--------|--------|--------|
| Merge any outstanding PRs to `main` | **Small** (hours) | Single maintainer workflow |
| Tag / release **v0.2.0** after doc merge | **Small** | Aligns with `CHANGELOG.md` |
| Kick off **48–72h sim** on DO droplet with fixed `BOT_EXPERIMENT_ID` | **Small** ops | Use `docs/VALIDATION_CHECKLIST.md` |

---

## 2. During the sim run

| Action | Effort | Notes |
|--------|--------|--------|
| Share dashboard stack details with dashboard team | **Small** meeting | Hand them **`docs/HANDOFF.md` §5** verbatim: structlog event names + Prometheus schema + label rules (`tif` vs `mode`) |
| Expose `/metrics` to scraper (localhost agent or firewall allowlist) | **Small** | See `deploy/DEPLOY_DO.md` §1 |
| Confirm log pipeline retains `experiment_id` field | **Small** | Required for weekly report and Loki dashboards |

---

## 3. Post-sim (before live)

| Action | Effort | Notes |
|--------|--------|--------|
| Walk `docs/VALIDATION_CHECKLIST.md`; file waivers | **Medium** | Engineering + risk sign-off |
| Run `weekly_report.py` on full window; archive JSON | **Small** | |
| Fix **correlation guard** keying if you intend to rely on it | **1 PR** (~0.5–1 day) | Not blocking sim if arb-heavy |

---

## 4. Dashboard integration

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
| **Correlation guard fix** | 1 PR | Risk correctness before scaling size |
| **Trade ledger DB** | Multi-day | Unblocks accurate reporting without log scrape |
| **pending_orders persistence** | Medium | If restarts during live cause pain |
| **Polygon settlement listener** | Multi-day | Latency-sensitive strategies only |
| **pUSD in wallet_reconcile** | Medium | Reduces false drift alerts |
| **Sharpe/Sortino in weekly report** | Small PR | After stable equity series / DB |

Honest sizing: **ledger DB + listener** are **multi-day refactors**; correlation fix and Sharpe are **single PR** scale.
