# Changelog

> **v0.2.0** marks **pre-dashboard readiness**: settlement, fills, arb unwind, metrics, reconciliation, weekly report, and docs are in place for the 48–72h sim run and dashboard integration.

## [0.2.0] — 2026-05-02

### Prompt 7 — Weekly performance report
- Added `scripts/weekly_report.py`: log-derived PnL, arb bundle FIFO accounting, strict `--experiment-id`, drawdown from equity series, JSON/Markdown output.

### Prompt 9 — On-chain equity reconciliation
- Added `utils/equity_reconciliation.py` (dual USDC bridged + native), hourly loop in `main.py`, `bot_equity_drift_pct` gauge, `equity_*` events.
- Added `scripts/wallet_reconcile.py` + `utils/wallet_reconcile_cli.py` (metrics scrape or log fallback, exit codes 0/1/2).

### Prompt 5 — Correlation data path
- Added `utils/fill_correlation.py`; `main.py` calls `record_fill_return_for_correlation` after fills (known limitation: `correlation_blocked` keying still buggy — see `docs/FINAL_CODE_REVIEW.md`).

### Prompt 3 — Arb partial-fill compensation
- `reconcile_arb_bundle` async task: `arb_bundle_balanced`, `arb_unwind`, `arb_unwind_skipped`, `bot_arb_unwinds_total`.

### Prompt 2 — Live fill reconciliation
- `CLOBExecutor` pending orders, REST polling, partial-fill terminal states, reconciliation logging.

### Prompt 1 — Settlement
- `resolution_polling_loop`, Gamma `poll_market_resolutions`, `market_resolved` events, degraded backoff + metrics.

### Prompt 8 — Prometheus
- `observability/metrics_exporter.py`, port `BOT_METRICS_PORT` (default 9091), fill counters with `tif` label (not `mode`).

### Prompt 6 — Config
- Removed legacy `config/config.py`; single `config/settings.py` + new env vars in `deploy/.env.example`.

### Prompt 4 — experiment_id
- Structlog context vars for `experiment_id` and `mode` in `run_bot`.

### Prompt 4.5 — Tests
- Pytest harness, `tests/conftest.py` py_clob stub, coverage for resolution, fills, arb, metrics, equity, weekly report.

### Documentation
- `docs/HANDOFF.md` rewrite, `docs/FINAL_CODE_REVIEW.md`, `docs/VALIDATION_CHECKLIST.md`, `docs/NEXT_STEPS.md`, `TESTING_CHECKLIST.md`.
- Add top-level `README.md` as onboarding signpost (post-Prompt 7 polish).
- Documentation polish: cron fix, validation criteria tightening, sample outputs in HANDOFF (`§5c`), `scripts/weekly_report_cron.sh`, `deploy/DEPLOY_DO.md` Step 5 alignment.

## Unreleased

_No unreleased changes._
