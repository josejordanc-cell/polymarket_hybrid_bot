# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.2.3] — 2026-05-03

### Added
- **`.env.example`** rewritten in block-comment style (one comment block above each variable, never inline). Replaces an older inline-comment-style template that taught the wrong pattern.
- **`.env.bak.*`** added to `.gitignore` to prevent accidental commit of backup files created when editing `.env` in place on a deploy host.

### Fixed
- **Inline comments parsed as values in `.env` (`discord_alert_failed`, `equity_reconciliation_disabled`).** The env loader treats everything after `=` as the value, including `# comment` text. Three lines on the deploy host had this bug:
  - `BOT_DISCORD_WEBHOOK_URL=            # Discord channel webhook URL` -> the URL-encoded comment was sent to Discord, producing `discord_alert_failed: "#%20Discord%20channel%20webhook%20URL"`.
  - `BOT_TELEGRAM_CHAT_ID=               # your chat/channel ID` -> same shape, harmless because Telegram token was also unset.
  - `BOT_POLYGON_RPC_URL=                # QuickNode / Alchemy Polygon mainnet URL` -> not the cause of the wallet error, but cleaned for consistency. (Wallet error was a separate empty `BOT_POLYGON_WALLET_ADDRESS=` issue, deferred.)

  Fixed on the running droplet by stripping inline comments and recreating the container (`docker compose up -d`, not `restart` — env vars are baked at container creation, not at process start). `discord_alert_failed` is gone; reconciliation now reports the cleaner reason `BOT_POLYGON_RPC_URL unset` instead of `Non-hexadecimal digit found`.

- **`market_cache_write_failed: [Errno 13] Permission denied`.** The bot runs as UID 1000 inside the container, but `/opt/polymarket_hybrid_bot` was cloned as root on the droplet. Documented the chown one-liner fix in `DEPLOY_DO.md`. The proper fix (running as a non-root deploy user) remains tracked for v0.3.0.

### Verified
- ✅ Discord error confirmed gone after `docker compose up -d` recreated the container at 2026-05-03T04:46:26Z.
- ✅ `strategy_loop_started` observed with 32 markets discovered.
- ✅ Container running stably at v0.2.2 image; v0.2.3 is docs + template only, no image rebuild required for the env-hygiene piece. (chown step does not require rebuild either.)
- ✅ chown quick-fix applied on production droplet at 2026-05-03T05:06Z; `market_cache_write_failed` verified gone, container healthy with 32 markets.

### Known issues unchanged from v0.2.2 (tracked for v0.3.0)
- Polymarket WebSocket endpoint returns HTTP 404 — REST polling fallback active.
- Binance reference feed returns HTTP 451 from US/DigitalOcean regions — set `DISABLE_BINANCE_FEED=1` to silence.
- Equity reconciliation still disabled until `BOT_POLYGON_WALLET_ADDRESS` and `BOT_POLYGON_RPC_URL` are filled in (deferred to live-mode switchover).

## [0.2.2] — 2026-05-02

### Fixed
- **Dependency resolution failure under web3 7.6.0.** v0.2.1's pin set was internally consistent for the conflict it solved (web3 6.x → 7.x), but pinned the rest of the eth-stack at versions that web3 6.x accepted and 7.x no longer does. The first build attempt on the deploy host failed with three sequential resolver errors (`eth-account==0.13.0` vs `web3 7.6.0` floor of `>=0.13.1`; then `eth-typing==4.4.0` vs floor of `>=5.0.0`; then transitively `eth-utils==4.1.1` and `eth-abi==5.1.0`). Resolution: bump all four to versions matching the 7.x line:
  - `eth-account` 0.13.0 → 0.13.7
  - `eth-typing` 4.4.0 → 5.2.1
  - `eth-utils` 4.1.1 → 5.3.0
  - `eth-abi` 5.1.0 → 5.2.0

  `eth-rlp==2.1.0` and `hexbytes>=1.2.0,<2.0.0` are unchanged — both already satisfied 7.x's floors.

### Verified
- ✅ End-to-end `docker compose up -d --build` succeeded on the production droplet (Ubuntu 24.04, 1 vCPU / 1 GB + 1 GB swap, NYC1).
- ✅ `strategy_loop_started` observed in container logs at 2026-05-02T23:24:41Z, with 26 markets discovered.
- ✅ Container running stably; REST polling fallback active for the WS 404 known issue.

### Known issues observed in running container (deferred to v0.2.3)
- `discord_alert_failed` — `.env` has the placeholder comment `# Discord channel webhook URL` being parsed as the value. Either set `DISCORD_WEBHOOK_URL` to a real webhook or remove the line.
- `equity_reconciliation_disabled: wallet_address_error: "Non-hexadecimal digit found"` — funder address env var contains stray characters.
- `market_cache_write_failed: Permission denied` — bot runs as UID 1000 but `/opt/polymarket_hybrid_bot` is owned by root. Fix is to chown the deploy tree or move the cache to `/app/data/`.

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

### Deprecated

- **`deploy/Dockerfile`** and **`deploy/docker-compose.yml`** — superseded by the **root** `Dockerfile` and `docker-compose.yml` (v0.2.1 multi-stage build, service `bot`, no obsolete `version:` key). Kept only for reference or old bookmarks; do not use for new deployments.

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
