## [Unreleased]

Post-v0.2.4 security and hygiene fixes. All six changes below are
deployed to production (sim mode) and verified end-to-end. A v0.2.5
release tag will be cut once the remaining Phase 1 / Phase 2 hardening
items in `CURSOR_TASKS_v0.2.4.md` land, so v0.2.5 is a single coherent
"post-tag hardening" release.

### Removed

- Dead PascalCase strategy modules (canonical snake_case versions
  have been authoritative since v0.2.1):
  `strategies/IntraArbitrage.py`, `strategies/MarketMaker.py`,
  `strategies/MomentumLatency.py`, `strategies/SmartMoneyCopy.py`.
- `data_feeds/spot_feed.py` — superseded by the per-venue feed
  implementation.
- Deprecated `deploy/` duplicates: `deploy/Dockerfile`,
  `deploy/docker-compose.yml`,
  `deploy/polymarket-hybrid-bot.service`. The canonical Dockerfile
  and compose live at the repo root; the bot runs under Docker.

### Changed

- `observability/metrics_exporter.py`: bound the Prometheus HTTP
  exporter to `127.0.0.1` so the metrics endpoint is no longer
  reachable from the public network interface, closing the
  unauthenticated-metrics exposure flagged in the v2 security review.
  (PR #9, commit 0623eb4 — "T1")
- `config/settings.py` and `.env.example`: capped
  `BotRiskConfig.kelly_fraction` at `0.50` (the upper bound was `1.0`
  i.e. full Kelly). The lower bound of `0.05` is preserved. The
  `.env.example` comment now documents the 0.05–0.50 range explicitly.
  (PR #10, commit 9af6254 — "T2")
- `deploy/rebuild.sh`: fail-fast guards added at the top of the
  script. The script now exits 1 with a remediation hint if `.env` is
  missing (with copy/edit/chmod instructions) or if `.env` is not mode
  600. The mode check uses a dual `stat` fallback (`stat -c '%a'` on
  Linux, `stat -f '%A'` on BSD/macOS) so the guard works for both
  droplet and local runs. The previous behaviour of auto-creating
  `.env` from `.env.example` has been removed because it was masking
  this failure mode. (PR #11, commit 0eccb66 — "T6")
- `main.py`, `config/settings.py`, `Dockerfile`, `docker-compose.yml`,
  `.env.example`: moved the markets discovery cache from
  `/app/.market_cache.json` (image root, read-only for the `bot` user)
  to `/app/data/market_cache.json`, configurable via the new
  `BOT_MARKET_CACHE_PATH` env var. Backed by a `market_cache_data`
  Docker named volume so the cache survives image rebuilds. The
  Dockerfile creates `/app/data/` owned by `bot:bot` before `USER bot`.
  Closes the recurring `market_cache_write_failed: [Errno 13]
  Permission denied` warning at every startup. (PR #14, commit
  82908ec — "T10")

### Added

- `data_feeds/binance_feed.py`: new `BOT_DISABLE_BINANCE_FEED`
  environment flag (accepts `1`/`true`/`yes`, case-insensitive). When
  set, `_run_venue` returns early for `venue == "binance"` only —
  Coinbase and other venues are unaffected — and emits a single
  `binance_feed_disabled` INFO log line with the reason. This is the
  real disable mechanism that was queued for v0.3.0 in the v0.2.4
  changelog. In production from a US/DO IP, enabling the flag
  eliminated ~1500 `spot_feed_tick_error` (HTTP 451) events per hour
  with zero impact on the strategy loop. `.env.example` documents the
  flag in a new block immediately after `BOT_BINANCE_SYMBOLS`.
  (PR #12, commit 6a2e9ed — "T11")

### Security

- The five changes in `### Changed` and `### Added` above each close a
  discrete finding from the v2 documentation security review. See
  `docs/SECURITY_REVIEW.md` §0.1.1
  ("Mitigation log — 2026-05-04 post-tag patches") for the per-finding
  status update.

## v0.2.4 — 2026-05-04

Security hardening release. No application code changes; all changes are
to the container image, deployment config, environment template, and
documentation.

### Added

- `SECURITY.md`: vulnerability disclosure policy, supported-version table,
  in-scope/out-of-scope criteria. (PR #5)
- `docs/SECURITY_REVIEW.md`: full v2 documentation security review (1331
  lines, OWASP Top 10 + CIS Docker Benchmark coverage, 14 ranked findings,
  mitigation log of the 2026-05-04 hardening session, deferred-files
  catalogue for the next pass). (PR #8)

### Changed

- `Dockerfile`: pinned `python:3.11-slim-bookworm` by digest
  (`sha256:ee710afcfb733f4a750d9be683cf054b5cd247b6c5f5237a6849ea568b90ab15`,
  queried 2026-05-04) for both builder and runtime stages. Added `tini`
  as PID 1 for clean SIGTERM handling, OCI image labels, and
  `COPY --chown=bot:bot . /app` so cache writes do not require runtime
  chown. (PR #5)
- `docker-compose.yml`: added `cap_drop: ALL`,
  `security_opt: no-new-privileges:true`, `mem_limit`, `mem_reservation`,
  `pids_limit`, dedicated `bot_net` bridge network, json-file log
  rotation, and an explicit non-root `user: "1000:1000"` pin as
  defense-in-depth. (PR #5) Then dropped `cpus` from 1.5 to 1.0 so the
  compose file is compatible with the production 1-vCPU droplet without
  a manual in-place patch. (PR #6)
- `.env.example`: relabelled secrets with `[SECRET]` markers, added
  `BOT_METRICS_BIND_HOST` and `BOT_METRICS_AUTH_TOKEN`, removed
  `BOT_RISK_BANKROLL_USD`'s default value (must be set explicitly per
  deploy), and rewrote inline `KEY=VALUE  # comment` patterns to put the
  comment on a separate line — this idiom was the root cause of the
  v0.2.2 `equity_reconciliation_disabled: Non-hexadecimal digit found`
  incident, since `python-dotenv` reads the comment as part of the
  value. (PR #5)
- `.gitignore`: added entries for `data/`, `logs/`, `reports/`,
  `wallet*.json`, `mnemonic*`, `private_key*`, `*.kdbx`,
  `.market_cache.json`, `.env.bak.*` and other secret/runtime-state
  globs. (PR #5)

### Removed

- `.env.example`: removed the entire `# --- Feature flags ---` section.
  `DISABLE_BINANCE_FEED` and `SUPPRESS_WS_404_WARNINGS` were documented
  in the template but never wired to the runtime — neither variable is
  read by any `*.py` file under `/app`. The misleading
  `Set DISABLE_BINANCE_FEED=1 to skip Binance entirely` comment near
  `BOT_BINANCE_SYMBOLS` was rewritten to explain the actual situation:
  pydantic-settings v2 silently ignores empty-list env values, so even
  the documented `BOT_BINANCE_SYMBOLS=[]` fallback does not currently
  disable the Binance feed. A real disable mechanism is queued for
  v0.3.0 (likely a top-level `os.getenv` flag in
  `data_feeds/binance_feed.py`). (PR #7)

### Security

- Image is now reproducible-by-digest end-to-end. Supply-chain
  resistance against `latest`-style tag mutation. Tracked rebuild
  cadence: on each upstream security advisory, or every 90 days.
- Container drops all Linux capabilities and disables privilege
  escalation. The bot needs none.
- Resource ceilings prevent a runaway loop or memory leak from taking
  the host down.
- Deploy key for the production droplet is registered as **read-only**
  on GitHub; the droplet cannot push.
- Production droplet (157.230.63.174) is hardened against the v1
  `CRITICAL` exposure: SSH key-only, fail2ban active, kernel current,
  unattended-upgrades enabled, repository flipped to private. Audit of
  the exposure window shows no successful login from outside the
  maintainer's residential ISP.

### Known issues (unchanged from v0.2.3)

- Polymarket WS 404 — REST polling fallback is active. Tracked for
  v0.3.0.
- Binance HTTP 451 from US/DO IPs — see the new comment in
  `.env.example` near `BOT_BINANCE_SYMBOLS`. Cosmetic noise at WARNING
  level only; bot strategy continues to function.
- Equity reconciliation requires `BOT_POLYGON_RPC_URL` to be set to a
  paid RPC provider (QuickNode/Alchemy/Infura). Live mode without
  reconciliation is **not safe**; see `docs/SECURITY_REVIEW.md` finding
  #5.
- Bot still deploys as root on the host. Documented as a known issue;
  hard live-mode gate is in `docs/HANDOFF.md`.

### Pull requests merged

- PR #5: security: v2 editorial revisions per SECURITY_REVIEW.md
- PR #6: fix(compose): cpus 1.5 -> 1.0 for 1-vCPU droplet
- PR #7: docs(env): remove fictional DISABLE_BINANCE_FEED and
  SUPPRESS_WS_404_WARNINGS
- PR #8 _(this release)_: docs: commit SECURITY_REVIEW.md v2 and
  CHANGELOG for v0.2.4

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
