# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.2.1] — 2026-05-02

### Fixed

- **Docker / pip dependency deadlock** — `py-clob-client==0.22.0` strictly pins `eth-account===0.13.0`, while `web3` 6.x required `eth-account<0.13`, which also surfaced **`hexbytes`** resolver conflicts. **Resolution:** bump **`web3` to `7.6.0`** (supports `eth-account>=0.13.0`) and pin **`hexbytes>=1.2.0,<2.0.0`**. Do **not** downgrade `eth-account` — installs can succeed and **`py-clob-client` fails at import** if the pin is violated.
- **Missing `scripts/__init__.py`** — Restored so `python -m scripts.weekly_report` and `from scripts.weekly_report import …` work reliably.
- **Obsolete Compose `version:` key** — Removed for Compose v2.

### Added

- **Multi-stage `Dockerfile`** — `python:3.11-slim-bookworm` builder (`build-essential`, `gcc`, `libffi-dev`, `libssl-dev`) installs into `/opt/venv`; runtime copies venv + `ca-certificates` + `tzdata`, runs as **`bot:1000`**.
- **`.dockerignore`** — Keeps secrets, caches, `logs/`, `data/`, and fat artifacts out of the build context.
- **`deploy/rebuild.sh`** — One-shot `down` → optional prune → `build --pull` → `up -d` → **60 s** poll for `strategy_loop_started`.
- **`HEALTHCHECK`** — File mtime check on **`/tmp/.bot_alive`** (touched by a background task in `main.py`).
- **`polymarket_hybrid_bot` package shim** — `CMD` uses **`python -m polymarket_hybrid_bot.main`** without moving strategy sources.
- **`scripts/weekly_report.py`** — JSONL **`trades.jsonl`** aggregate report with **empty / missing file** handling (no exception).

### Changed

- **`requirements.txt`** — Exhaustive v0.2.1 pin set for the Ethereum / CLOB stack; additional **`pydantic`**, **`structlog`**, **`ccxt`**, **`prometheus-client`**, and **pytest** lines retained for this repository’s application code and tests.
- **Documentation** — Rewrote `README.md`, root `HANDOFF.md`, root `DEPLOY_DO.md` for v0.2.1 workflows.

### Known issues (carried forward — not introduced in v0.2.1)

- **Polymarket WebSocket 404** on upgrade — bot uses **REST polling** fallback.
- **Binance HTTP 451** from US / many DO regions — document **`DISABLE_BINANCE_FEED=1`** and region selection.

### Verification

- ✅ `python -c "from scripts.weekly_report import generate_report, WeeklyReport; …"`  
- ✅ `pytest tests/test_weekly_report.py`  
- ✅ `bash -n deploy/rebuild.sh`  
- ⚠️ **Full `docker compose build` + `strategy_loop_started`** — confirm on the **deploy host** as the first acceptance gate.

---

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
