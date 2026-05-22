# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### v0.2.9 — Phase 3 wire-shape alignment + snapshot history wiring

Bot side of the dashboard contract reconciled with the canonical Zod
source in `~/crypto-trading-bot-dashboard/components/dashboard/schemas.ts`
+ `BOT_CONTRACT.md` v1.0. Two-team swarm: Team B (this repo)
authored the bot-side rewrites; Team A handled the parallel dashboard-side
work. See `.swarm-decisions.md` for per-sub-phase decision log entries.

#### Wire-shape alignment (Phase 3, sub-phases 3a–3i)

- **3a** — `globalStats` field rename + `lastUpdatedAt` relocation
  (totalEquity → portfolioValue, dayPnl → pnlToday; lastUpdatedAt moved
  from DashboardSnapshot root into GlobalStats per audit A.2).
- **3b** — `StrategyConfig` + `riskPct` semantic. `id` → `strategyId`,
  `riskBudget` (0.0–1.0 fraction) → `riskPct` (0.1–10.0 percent),
  `activePairs` CSV → list[str] of BASE/QUOTE, default `riskPct=2.5`.
  Narrow L2 unfreeze on `api/state.py` for the rename.
- **3c** — Six top-level `DashboardSnapshot` key renames per audit A.1
  (openPositions → positions, recentFills → fills, riskMetrics → risk,
  equityHistory → equity, recentLogs → logs, recentAlerts → alerts).
- **3d** — `positions[]` live wiring + `_collapse_position_id` boundary
  helper (Polymarket `marketId:outcome` / spot `venue:symbol`).
- **3d-fix** — `OpenPosition` canonical 11-field Zod shape (side →
  direction title-case, quantity → size, openedAt → timeOpen ISO-8601-Z,
  unrealizedPnl → unrealizedPnlUsd; added tradeId). Forward-only
  correction; the 3d landing followed the deprecated audit B.3 field set.
- **3d-pre** — Live-blocker microfix: `activePairs` ships `[]` not null,
  `lastUpdatedAt` ships `int(time.time() * 1000)` not 0 sentinel.
- **3e** — `RiskMetrics` canonical 6-field Zod shape (maxDrawdown,
  sharpe, var95, leverage, exposurePct, liquidationRisk). leverage=1.0
  spot-venue structural constant; other 5 default None pending B.5
  full-wiring. Drops the v0.2.8 legacy 6-field stub set.
- **3f** — `EquityPoint` (B.7), `BotLogEntry` (B.8), `AlertItem` (B.9)
  canonical Pydantic shapes. Option C lossy log-level map at the
  serializer (debug → drop, critical → ERROR collapse). E.3 alert
  severity collapse (error → critical). EXEC vestigial slot in Zod log
  enum; bot never emits it.
- **3g** — Cross-team recon only (no bot edits). Confirmed no TICK
  publish path existed pre-3i; Team A then added Zod `.refine()` on
  PriceTickSchema for the eventual 3i landing.
- **3h** — `_build_open_position` helper promotes the 3d-fix
  paste-and-fill recipe to live code; `positions=[]` stub replaced with
  list-comp. Adds `POST /debug/inject-position` + `POST
  /debug/clear-positions` (sim-mode-only). Narrow L2 unfreeze on
  `api/state.py` extended.
- **3h-fix** — `_verify_debug_mode` reads `config.runtime.mode` (not
  `config.mode`); MagicMock auto-attribute creation had masked the bug
  in tests.
- **3i** — `PriceTick` canonical 5-field Zod shape (symbol, last, bid?,
  ask?, ts epoch ms). Adds production TICK WS emit in
  `data_feeds/binance_feed._record` with per-(venue, symbol) 250ms
  throttle. bid/ask ship null in v1 (no SpotTick struct change).
  ts = `int(utc_now().timestamp() * 1000)` 13-digit, satisfies Team A's
  3g `.refine(v >= 1e12 && v < 1e14)` guard.

#### Snapshot history wiring (BotState writers for previously-stub slices)

- **B.9 alerts** — `utils.alerts._broadcast_dashboard` atomically
  appends a wire-shape AlertItem to `BotState.recent_alerts` under
  `state.write_lock` BEFORE the WS broadcast. Reconnecting client sees
  alert in snapshot even if WS frame in-flight. New
  `api.state.set_state` / `get_state` module-level accessors (mirrors
  `utils.alerts.set_emitter` precedent); registered in `api/server.py`
  alongside emitter wiring. Snapshot path's `alerts=[]` stub flipped to
  `list(snap.recent_alerts)`.
- **b9-fix** — `POST /debug/inject-alert` endpoint (sim-mode-only)
  fires `send_alert` internally so the full B.9 production path
  (state.recent_alerts + WS broadcast) is exercisable via curl. Same
  5 security gates as `inject-position`. `test_client` fixture also
  updated to call `set_state(bot_state)` so the singleton is
  registered in tests (production wiring at `api/server.py` was
  already correct).
- **B.8 logs** — New `utils/logs.py` module with `log_to_dashboard`
  PATH-A opt-in helper. State-only (no WS broadcast — Zod
  `WsMessageSchema` has no LOG variant). 3f Option C lossy level map
  applied inside helper. `source` folded into `message` as
  `[{source}] {message}` (4-field BotLogEntry has no source field).
  `_LOG_LEVEL_MAP` + `_map_log_level` moved from `api/routes_rest.py`
  (3f speculative landing) to `utils/logs.py` natural home;
  `TestMapLogLevel` test class moved alongside. Snapshot path's
  `logs=[]` stub flipped to `list(snap.recent_logs)`. New `POST
  /debug/inject-log` endpoint (sim-mode-only); returns 204 No Content
  on `level="debug"` (intentional drop per Option C). No existing bot
  paths hooked to `log_to_dashboard` in this round; callsite curation
  deferred.
- **B.7 equity** — New `utils/equity_curve.py` module with
  `equity_snapshot_loop` background task. Bootstrap-append on loop
  start so a freshly-restarted bot returns a non-empty
  `equity_history` from `GET /dashboard/snapshot` within seconds; then
  hourly tick with UTC-day-rollover detection (one EquityPoint per
  calendar day, `pnl = state.equity - state.day_start_equity`).
  Lifecycle: created in `api/server.py:run()` alongside emitter/state
  wiring, cancelled + awaited in finally block. `BotState.equity_history`
  promoted from unbounded list to `deque(maxlen=200)` (~6.5 months of
  daily-close history). Snapshot path's `equity=[]` stub flipped to
  `list(snap.equity_history)`.

#### Tests

- Test count: 144 (post-3a baseline) → 303 (post-B.7). Net +159 over
  the v0.2.9 wave across schema round-trips, helper unit tests,
  endpoint integration tests, and lifecycle / cancellation tests.
- New `tests/api/test_schema_contract.py` is now the canonical
  cross-team schema-drift guard (verifies field sets + envelope shapes
  against the dashboard's canonical contract).

#### Removed / deprecated

- Audit row B.3 (10-field OpenPosition w/ side / quantity / openedAt
  / unrealizedPnl) deprecated by A-2 reconciliation matrix; canonical
  11-field Zod shape is authoritative (3d-fix).
- Audit row B.5 (10-field RiskMetrics) deprecated; canonical 6-field
  Zod shape is authoritative (3e).

### v0.2.9 follow-ups (post-wave)

Follow-up commits landing after the main v0.2.9 wave above. All keep
the v0.2.9 wire contract intact; the changes are wiring promotions,
a renamed-for-accuracy class, formal stub closes, and an honest
production fix.

#### Added

- **`monthlyPnl` wired** (commit `674d254`) — promoted from OQ-3 stub
  to LIVE at the snapshot serialize boundary via
  `utils.equity_curve.compute_monthly_pnl(snap.equity_history)`. No
  schema/field/type change; the field flipped from always-emitting
  `None`/`[]` to an actual rolling aggregate over the equity-history
  deque already populated by `equity_snapshot_loop` (B.7).

#### Changed

- **`BinanceFeed` → `SpotFeed`** (commit `c78ff69`) — full rename
  (class + module + test file): `data_feeds/binance_feed.py` →
  `data_feeds/spot_feed.py`. The class has been multi-venue
  (Binance + Coinbase via ccxt.pro) since v0.2.x; the Binance-only
  name was vestigial from v0.2.0. **Preserved unchanged** (operator
  surfaces): `BOT_DISABLE_BINANCE_FEED` env flag,
  `binance_feed_disabled` log event, `binance_spot` /
  `coinbase_spot` asyncio task labels — operators may have grep
  scripts keyed on these.

- **Conftest CLOB-stub guard tightened** (commit `0e9b962`) — the
  test-suite `py_clob_client` fake stub previously only checked
  `import py_clob_client.client` (package-existence), which masked
  API drift. The guard now probes for every symbol the bot's import
  block expects so failing any one triggers the stub.
  Post-`d043038` the guard returns `True` against
  `py-clob-client==0.22.0` and the stub does not fire.

#### Fixed

- **CLOB SDK API drift** (commit `d043038`, ref
  `v029-clob-sdk-drift`) — the bot imported `Side` from
  `py_clob_client.order_builder.constants`, which is not exported by
  the installed `py-clob-client==0.22.0` (the module only exposes
  bare `BUY = "BUY"` / `SELL = "SELL"` strings, and
  `OrderArgs.side: str`). The `ImportError` was swallowed by the
  broad `except Exception` guard, set `_CLOB_AVAILABLE = False`, and
  surfaced in live mode as the misleading
  `"py-clob-client-v2 is not installed"` RuntimeError. The five call
  sites (`execution/clob_executor.py` + four strategy modules) now
  pass the bare `intent.side` (already
  `Literal["BUY","SELL"]`) directly to `OrderArgs.side`; the
  `Side.BUY if ... else Side.SELL` ternary was a no-op rewrap. The
  RuntimeError text was rewritten to name the real failure mode.
  Sim mode was unaffected; live mode on the droplet was broken.

#### Documented

- **Backtest + bid/ask formally closed as intentional deferred
  stubs** (commit `ed1a5fc`, refs `v029-backtest-close` and the
  bid/ask close row in `.swarm-decisions.md`). `BacktestPoint` ships
  `[]` from production because `BacktestEngine` lacks per-strategy
  equity attribution — this is a feature-build task, not a wiring
  task, and the engine's aggregate `equity_curve` cannot back-fill
  the dashboard's 4-field per-strategy shape. `PriceTick.bid` /
  `.ask` ship `null` per 3i lock 4; Zod
  `PriceTickSchema.optional()` accommodates that perpetually, and no
  dashboard consumer reads either field today.

#### Tests

- Test count: 303 (post-B.7 baseline) → **376** (post-`d043038`),
  with the 6+ live-fill-reconciliation tests now running against the
  REAL SDK (not the conftest fake stub). Verified locally with
  `pytest tests/` → 376 passed.

### Earlier in the [Unreleased] window — Phase E.1 (pre-v0.2.9 wave)

Strategy-id translation at the FILL wire boundary and BotState
truthfulness for env-driven strategy flags. Branch
`feat/intra-arb-dashboard-integration`, +2 commits over main.

#### Added (Phase E.1)

- **B.1 — FILL strategy-id translation** (commit `b3558ef`): `bot_to_dashboard()` /
  `dashboard_to_bot()` helpers in `api/schemas.py`. FILL payload in `utils/fills.py:108`
  now emits `strategy1`/`strategy2`/`strategy3` (dashboard contract ids) instead of the
  internal strategy name. `smart_money_copy` raises `ValueError` at the wire boundary
  (no contract id in v1.0). Docstring TODO added at `utils/strategy_updates.py` noting
  the STRATEGY_UPDATE emit site is deferred to v0.2.9.
- **B.2 — BotState reflects env strategy flags** (commit `6da5abb`): `BotState.from_risk`
  now reads `config.strategy.momentum_enabled` and `config.strategy.market_maker_enabled`
  so the snapshot `status` field for strategy cards reflects actual config truth, not a
  hardcoded stub. `_build_snapshot` iterates an explicit tuple for deterministic card
  order (reviewer Finding 6 fold-in). Reviewer Finding 6 from the Phase E.1 review.

---

## [0.2.5]

Post-v0.2.4 security and hygiene fixes. All changes below are deployed to production
(sim mode) and verified end-to-end.

Task **T7** (PR #17) is summarised under `### Changed` below. Task **T9**
(PR #18) is also under `### Changed`. Task **T12** (PR #19) is under `### Added`.

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
- `main.py`: strategy loop liveness heartbeat. Emits
  `strategy_loop_started` once on loop entry and a
  `strategy_loop_heartbeat` event every 300 iterations
  (~60s at the 0.20s strategy interval) with `iterations`,
  `markets_evaluated_total`, `orders_submitted`. Closes the
  observability gap where the loop's `while not _SHUTDOWN`
  was silent for arbitrary durations between order events.
  The follow-up commit `7f7f934` adds `nonlocal
  _hb_orders_submitted` inside the nested
  `_exec_and_record` async function — without it, the
  counter increment would have raised `UnboundLocalError`
  the first time any strategy submitted an order.
  (PR #19, commits 415b04c + 7f7f934 — "T12")
- Per-feed liveness heartbeats every 60s. `spot_feed_heartbeat`
  (binance_feed, per active venue, with `ticks_seen`,
  `last_price`, `seconds_since_last_tick`) and
  `polymarket_feed_heartbeat` (with `markets_subscribed`,
  `messages_seen`, `seconds_since_last_message`). WARNING level
  when stale > 30s to double as a stall indicator (T13).
- LICENSE file at repo root — proprietary "All Rights Reserved" for Jose Jordan, 2026 (PR #30, commit 8b1a517).
- Project-level CLAUDE.md at repo root — agent discipline rules and observability invariants anchored to docs/HANDOFF.md §5 (PR #31, commit 838f928).
- **D.2.2 step 1:** Add PyJWT>=2.8.0 direct dep for HS256 WS-ticket verification (api/auth.py, used in D.2.3).
- **D.2.2 step 2:** Pydantic v2 schemas for BOT_CONTRACT.md Appendix B shapes (api/schemas.py, 19 models incl. WS discriminated union) and BotState dataclass with asyncio.Lock, bounded deques, shallow_copy(), MagicMock-safe from_risk(), and apply_strategy_patch() helper (api/state.py).
- **D.2.2 step 3:** Bearer and WebSocket ticket auth helpers (api/auth.py): verify_bearer using hmac.compare_digest, verify_ws_ticket using PyJWT HS256 with single-use nonce store (10k-entry FIFO-by-exp eviction, threading.Lock).
- **D.2.2 step 4:** REST handlers and server entry point. api/routes_rest.py implements three handlers: GET /dashboard/snapshot, POST /api/strategies/config, POST /emergency-stop — all Bearer-auth gated via hmac.compare_digest. Snapshot handler uses BotState.shallow_copy() to avoid lock contention; write handlers acquire write_lock. api/server.py provides a run() coroutine wrapping aiohttp AppRunner with shutdown-event polling and graceful teardown.
- **D.2.2 step 5:** tests/api/ suite — 97 unit and integration tests covering schemas, state, auth, all three REST handlers, and server lifecycle. Mock-only (no real RiskManager). Uses aiohttp.test_utils.TestClient; no new pytest dependencies introduced.
- **D.2.2 step 6:** HttpApiConfig nested model added to config/settings.py under BotConfig.http_api (env prefix BOT_HTTP_API_). Fields: enabled (bool, default False), bind_host, port (1–65535 validated), bot_api_token (SecretStr), ws_ticket_secret (SecretStr). Model validator requires both secrets present when enabled=True; disabled by default so existing deployments are unaffected. main.py constructs BotState unconditionally in run_bot() before the task graph and appends the api.server.run task conditionally when http_api.enabled is True (lazy import keeps aiohttp off the cold-start path when disabled).
### Changed

- T14: `healthcheck.py` — replace Binance API ping (always 451 from
  droplet) with spot-feed liveness check based on T13 heartbeat events.
  Renames check from `binance_api` to `spot_feed`. Disabled venues (T11)
  no longer cause false-negative degraded states.
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
- `utils/alerts.py`: dropped `parse_mode: "HTML"` from the
  Telegram webhook payload so untrusted log fields and market
  symbols can no longer break formatting or inject HTML
  entities. Body shape is otherwise unchanged; messages render
  as plain text. (PR #17 — "T7")
- `config/settings.py`, `data_feeds/polygon_feed.py`: added a
  `model_validator` on `StrategyConfig` that refuses to load
  when `BOT_STRAT_SMART_MONEY_ENABLED=true` while the wallet
  indexer (`data_feeds/polygon_feed.py:_scan_recent_wallets`)
  is still a stub. Default config (smart-money disabled)
  loads cleanly; setting the env flag to true now produces a
  pydantic `ValidationError` at startup mentioning
  "_scan_recent_wallets" and "stub", instead of the strategy
  silently scanning an empty wallet list. Cross-reference
  comment added in `polygon_feed.py` near the stub.
  (PR #18, commit 87ccd71 — "T9")

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

### Security

- The nine changes in `### Changed` and `### Added` above each close a
  discrete finding from the v2 documentation security review (among
  them T7, T9, T12, T13). See
  `docs/SECURITY_REVIEW.md` §0.1.1
  ("Mitigation log — 2026-05-04 post-tag patches") for the per-finding
  status update.
- **T4:** Add ten pydantic format validators in `config/settings.py` for env vars so malformed
  values produce hard boot errors. Covers URL scheme (host/ws/rpc), hex format (private key,
  wallet address), minimum length (api_key, api_secret), non-empty (api_passphrase), symbol
  pattern (binance/coinbase BASE/QUOTE), and log-safe chars (experiment_id). Empty-value
  short-circuits preserve sim mode behavior. Existing T3 `_live_mode_requires_keys` validator
  continues to enforce non-emptiness for live mode. Tests fixture in
  `tests/test_live_fill_reconciliation.py::live_env` updated in the same PR (api_key and
  api_secret bumped from 12/11 chars to 32 chars).
- **T3:** Convert four Polymarket credential fields (`polymarket_private_key`, `polymarket_api_key`, `polymarket_api_secret`, `polymarket_api_passphrase`) and `polygon_rpc_url` from `str` to `pydantic.SecretStr`. Defense-in-depth: SecretStr auto-redacts in `repr()`, `model_dump()`, and `__str__`, so a future logging mistake won't print live credentials. Twelve callsites updated to call `.get_secret_value()` when extracting cleartext (4 in settings.py validator body, 4 in clob_executor ClobClient construction, 1 in polygon_feed RPC init, 1 in main.py reconciliation loop, 1 in wallet_reconcile_cli, 1 in equity_reconciliation).
- **T5:** Fix `correlation_blocked` silent no-op in `risk_manager.py:190-213`. The guard iterated `_state.open_positions` keys (composite `{market_id}_{outcome}`) and looked them up in `_return_history` (keyed by bare `market_id`), so every lookup returned `[]` and the `n < 20` early-out always fired — non-arb strategies got zero correlation protection. Fix (option c): iterate `self._state.open_positions.values()` and read `Position.market_id` (zero new state, matches existing access pattern at `main.py:99,110`). Verification: 4 new unit tests in `tests/test_correlation_blocked.py` (correlated→True, uncorrelated→False, n<20→False, self-market→False, seeded numpy); strengthened `tests/test_record_return_correlation.py::test_approve_orders_reaches_correlation_guard` (now asserts `approved == []`); revert-proof smoking-gun test failed on clean revert and passed on staged code; local pytest 24/24 in full suite, 7/7 targeted; droplet smoke 2026-05-08 (`polymarket_hybrid_bot:0.2.4`, Up >2 min healthy, 3 heartbeats firing on cadence, in-container `pytest tests/test_correlation_blocked.py tests/test_record_return_correlation.py` 7/7 PASSED in 0.73s, bidirectional smoke `T5_SMOKE_OK`, 3-minute log scan zero tracebacks). **Note: this fix converts the guard from a silent no-op to actively evaluating** — expect `correlation_block` log events (`logger.warning("correlation_block", ...)` at `risk_manager.py:206-211`) to start appearing in sim post-deploy. That is the guard finally working, not a regression. Threshold tuning (`cfg.risk.max_pair_correlation`, default `0.80`) is a separate post-T5 concern before the live flip. (PR #28, commit `704ddcc`)
- **SECURITY.md contact email:** Replace the `<configure-a-dedicated-security-email-and-paste-here>` placeholder on line 23 of `SECURITY.md` with a dedicated security contact alias. GitHub Private Vulnerability Reporting remains the preferred channel; the email is the documented fallback. Closes one of the three live-flip prerequisites tracked in the session handoff. Scope: docs only, no behavior change. (PR #34, commit `591322a`)
- **T15:** Harden `.dockerignore` with wallet/keystore/credential globs (`*.p12`, `*.pfx`, `*.kdbx`, `wallet*.json|dat|txt`, `mnemonic*`, `private_key*`, `private-key*`, `keys/`, `keystore/`, `keystores/`, `*.keystore`, `UTC--*`). Belt-and-suspenders defense: `Dockerfile:54` does a wholesale `COPY --chown=bot:bot . /app`, so `.dockerignore` is the only barrier between the working tree and the runtime image. The previous rule set (last touched at v0.2.1, commit `554e2a8`) predated the wallet+credential work landed in T3/T4 and missed several artifact shapes already covered in `.gitignore`. Globs are suffix-scoped so source files like `utils/wallet_reconcile_cli.py` and `scripts/wallet_reconcile.py` remain in the build context. Verification: static glob check (`/tmp/t15_verify_dockerignore.py`) confirms 28 sentinel paths excluded and 11 source/template paths preserved; image-content audit deferred to next planned droplet redeploy.
- **T19:** Hash-pin `requirements.txt` via `pip-compile --generate-hashes` from new `requirements.in` source-of-truth file, and add `--require-hashes` to the Dockerfile pip install step. Every direct + transitive dep now carries `--hash=sha256:...` entries (2136 lines, 73 packages, generated from 29 direct deps in `requirements.in`). The eth-stack protected set (CLAUDE.md hard rule 7) is preserved exactly: `py-clob-client==0.22.0`, `eth-account==0.13.7`, `web3==7.6.0`, `eth-typing==5.2.1`, `eth-utils==5.3.0`, `eth-abi==5.2.0`, `eth-rlp==2.1.0`, with `hexbytes` bounded-then-locked at `1.3.1` (within the `>=1.2.0,<2.0.0` band specified in `requirements.in`). `ccxt[async]>=4.2.0` extras syntax preserved end-to-end (`--strip-extras` deliberately omitted from the pip-compile invocation to avoid silently dropping aiohttp's asyncio integration). Generation toolchain: `pip 25.2` + `pip-tools 7.5.3` on Python 3.11. Acceptance gate: `./deploy/rebuild.sh` on droplet per hard rule 12 — local sandbox cannot validate linux/amd64 wheel hashes.

### Notes
- Both changes are docs/meta-only. No behavioral change. No droplet redeploy required. Image digest unchanged.

## [0.2.8] — 2026-05-18

D.2.4 emit-site swarm (ALERT, FILL, STRATEGY_UPDATE batches). Two of three batches landed contract-conformant; STRATEGY_UPDATE wire-up was caught contract-nonconformant by post-implementation empirical verification and was reverted before the v0.2.8 tag moved to its final HEAD.

### Added

- **ALERT emit site** (D.2.4 step 1, SHA `4f63d9b`): `utils/alerts.send_alert` now broadcasts a `WsAlert` envelope to dashboard subscribers via a module-level Emitter singleton, in addition to the existing Telegram/Discord and `state.recent_alerts` sinks. Empirically verified against the dashboard's zod `WsMessageSchema` at b26eb3d — happy path parses cleanly and the dashboard reducer applies the alert to `alertsStore`.
- **FILL emit site** (D.2.4 step 2, SHA `a98f3ea`): `main._exec_and_record` broadcasts a `WsFill` envelope after the existing `risk_manager.on_fill` and Prometheus `record_fill` sinks. `FillRecord.pnl` is `Optional[float]` (emits `null` at fill time; dashboard `FillSchema.pnl` is `.nullable()` — the FILL-style Level-1 escalation pattern). Empirically verified the same way as ALERT: happy path (`pnl=null`), pnl-known path (`pnl=42.5`), and `status="pending"` edge case all parse cleanly.
- **StrategyCardData reshape** (`api/schemas.py`): contract-aligned per `BOT_CONTRACT.md:113`. `status` is `Literal["live", "paused"]`; 7 telemetry fields (`pnlToday`, `pnlMonth`, `winRate`, `tradesToday`, `avgHoldMinutes`, `lastSignal`, `exposure`) are `Optional` (bot emits `null` at snapshot time for unstarted strategies); `sparkline: list[float]` defaults empty. `_build_snapshot` simplified to match. Dashboard companion at `crypto-trading-bot-dashboard@6a6774e` made the corresponding zod fields `.nullable()` and added null-safe rendering across 5 components.
- **Schema-contract regression tests** (`tests/api/test_schema_contract.py`): ALERT/FILL envelope-flatness snapshots, `FillRecord.pnl` is-required-False invariant, STRATEGY_UPDATE envelope-flatness. STRATEGY_UPDATE field-set test rewritten to assert against the LITERAL contract text in `BOT_CONTRACT.md:609`, not against the bot's pydantic model (discipline lesson — see Reverted below).
- **Broadcast infrastructure** (`utils/strategy_updates.py`): sync-from-async bridge via `asyncio.get_running_loop().create_task(...)`, module-level Emitter singleton mirroring `utils/alerts.py` and `utils/fills.py`. 5 unit tests in `tests/utils/test_strategy_updates.py`. **No correct emit site wired in v0.2.8** — see Reverted below; v0.2.9 will wire from the future strategy-telemetry pipeline.

### Reverted (pre-final-tag)

- **STRATEGY_UPDATE wire-up** at `api/state.py:BotState.apply_strategy_patch` (introduced `d1d2276`, reverted in v0.2.8 post-tag correction). The bot was wired to emit `{enabled, maxPositionSize, riskBudget, activePairs}` (StrategyConfigPatch fields). `BOT_CONTRACT.md:413, 609` specifies `STRATEGY_UPDATE.updates` as a partial of `StrategyCardData` (card-telemetry fields): `name, status, pnlToday, pnlMonth, winRate, tradesToday, avgHoldMinutes, lastSignal, pipelineStep, exposure, sparkline`. The two field-sets are disjoint at the field-name level. The dashboard's zod `StrategyCardUpdatesSchema.partial()` silently strips unknown keys, so the bot's emission produced `updates: {}` after parse and the reducer applied a no-op spread `{...row, ...{}}`. Wire-valid, contract-nonconformant, silent functional no-op. Caught by empirical round-trip testing against the dashboard's actual zod schemas at `crypto-trading-bot-dashboard@b26eb3d`.
- **StrategyUpdateFields shape** reshaped from the 4-field `StrategyConfigPatch` mirror to the contract-correct 11-field partial of `StrategyCardData` (all `Optional`). Contract-correct but no caller exists; ready for the v0.2.9 emit site.
- **`_set_strategy_updates_emitter` registration** removed from `api/server.py` (no current caller, so no need to register the helper at server startup; `utils/alerts` and `utils/fills` registrations retained).
- **3 sync `apply_strategy_patch` tests** in `tests/api/test_state.py` reverted to minimal pre-d1d2276 bodies (no mock-emitter setup). The async `test_apply_strategy_patch_broadcasts_when_loop_running` test was deleted (the broadcast behavior it asserted no longer exists).
- **STRATEGY_UPDATE field-set regression test** in `tests/api/test_schema_contract.py` renamed from `test_strategy_update_fields_match_bot_runtime_patch_shape` to `test_strategy_update_fields_match_contract_card_partial`, with the snapshot constant moved from `_BOT_STRATEGY_UPDATE_FIELDS` (4 wrong fields) to `_CONTRACT_STRATEGY_UPDATE_FIELDS` (11 correct fields, copy-pasted from `BOT_CONTRACT.md:609`). The "permissive superset, semantic not syntactic" prose rationalization was removed.

### Documentation

- **Audit doc `docs/audit/d24-emit-site-audit.md`**:
  - §6.0 expanded from "Two strategy surfaces" to **"Three strategy surfaces"** — adding the card-telemetry display surface (`StrategyCardData` partial, the contract's `STRATEGY_UPDATE.updates`) alongside boot-time `config.strategy.*` and runtime `state.strategy_configs[<id>]`. The d1d2276 wiring confused surface (2) for surface (3); future emit-site work must identify which of the three it's emitting from.
  - §9.3 rewritten with explicit landed-vs-reverted breakdown.
  - §10 (continued, again) STRATEGY_UPDATE narrative gained a "**2026-05-18 correction**" block at the top of the section identifying what's wrong and what's still factually accurate in the historical narrative. The "Process note" at the end was rewritten to strike the bogus "STRATEGY_UPDATE-style escalation pattern" and add a discipline lesson: **contract-regression tests must assert against the literal contract text in `BOT_CONTRACT.md`, never against the bot's own pydantic model.** Bot-against-bot snapshots are self-consistent with whatever the bot emits — including the wrong thing.
- **Dashboard `PROJECT_HANDOFF.md`** (companion commit at `crypto-trading-bot-dashboard@b55431b`): walks back the "D.2.4 COMPLETE" framing. ALERT + FILL are complete; STRATEGY_UPDATE emit-site landed contract-nonconformant and was reverted; v0.2.9 will wire the correct emit site from the strategy-telemetry pipeline.

### Notes

- **v0.2.8 tag was moved.** The original tag at `816354b` claimed "D.2.4 emit-site wiring complete across all three WsMessage types," which was inaccurate for STRATEGY_UPDATE. Tag was deleted locally + on origin and re-anchored at the post-correction HEAD with an accurate message describing what actually shipped.
- **Production droplet at `157.230.63.174:/opt/polymarket_hybrid_bot` remains on v0.2.7.** Redeploy to v0.2.8 pending; D.3 droplet smokes will use the corrected tag.
- **v0.2.9 scope (forward)**: wire the correct STRATEGY_UPDATE emit site from the strategy-telemetry pipeline (whichever module produces `StrategyCardData` deltas downstream — does not exist today). The bot's `StrategyUpdateFields` is contract-correct and ready; `utils/strategy_updates.broadcast_strategy_update` is ready; `api/server.py` `_set_strategy_updates_emitter` registration will be re-added when the caller lands. Separately, the E.1-E.5 cross-team-L2 items catalogued in the dashboard repo's `docs/audit/d3-schema-audit.md` (pre-existing `GlobalStats` / `OpenPosition` / `StrategyConfig` / etc. mismatches not introduced by v0.2.8) remain open work items.

### Tests

164 passed (was 165 pre-correction; the deleted `test_apply_strategy_patch_broadcasts_when_loop_running` accounts for the delta).

---

## [0.2.7] — 2026-05-13

D.2.3 — WebSocket broadcaster module wired on top of v0.2.6's REST API. REST contract frozen since v0.2.6 (`api/schemas.py` blob `8ccdb63c`, `api/routes_rest.py` blob `8ae836cf`).

### Added

- **D.2.3 step 1:** api/emit.py — Emitter broadcaster for WebSocket fan-out. Owns the registry of connected WS clients keyed by opaque connection_id, exposes register/unregister/broadcast/stream_to_client. Per-client bounded asyncio.Queue (default maxsize=256). Back-pressure policy (D.2.1 §D-5, simplified per D.2.3 brief): successful put_nowait resets a per-client strike counter; three consecutive asyncio.QueueFull events schedule that client's ws.close(code=1011) + unregister in the background, leaving other clients unaffected.
- **D.2.3 step 2:** api/routes_ws.py — WebSocket route handler. Reads ticket from ?ticket=<jwt> query parameter and gates the upgrade through api.auth.verify_ws_ticket (HS256-only, exp + iat + single-use sub). On any auth failure (missing / bad-signature / expired / replayed / wrong-algorithm) the upgrade completes and the client receives a uniform WebSocket close frame with code 4001 "auth_required" per BOT_CONTRACT.md §3.2 (failure modes are deliberately indistinguishable). On success, registers with the Emitter, spawns a per-connection streamer task that drains the queue to ws.send_json, and runs an inbound log-and-drop loop. The handler's finally block owns the unregister lifecycle.
- **D.2.3 step 3:** tests/api/test_emit.py (10 tests) + tests/api/test_routes_ws.py (8 tests) — register/unregister cycles, broadcast fan-out to N=0/1/3 clients, backpressure strike counter (increment, reset on success, 3-strike disconnect), peer isolation under back-pressure, five 4001-close paths (missing / bad-sig / expired / replay / HS512), happy-path upgrade + broadcast delivery, client-initiated close → unregister. WebSocketResponse is mocked with AsyncMock for the unit suite; the route tests spin up a real aiohttp TestClient + TestServer. Full tests/api/ run: 115 passed (97 D.2.2 carry-over + 18 new).
- **D.2.3 step 4:** api/server.py wiring — instantiates Emitter in app[EMITTER_KEY], registers GET /ws via app.router.add_get, and replaces ad-hoc app[...] string keys with typed aiohttp AppKey to eliminate 16 NotAppKeyWarning lines at startup. (commit dcd6b51)

### Fixed

- **D.2.3 step 5 hotfix:** api/auth.py and api/server.py were reading `getattr(config, "api", None)` — the legacy ApiConfig — instead of `getattr(config, "http_api", None)` where D.2.2 step 6's HttpApiConfig (bot_api_token + ws_ticket_secret + bind_host + port) actually attaches at BotConfig.http_api with env prefix `BOT_HTTP_API_`. Production droplet smoke against /dashboard/snapshot returned 401 with log event `bearer_verify_config_missing`; the bug was masked by all 97 D.2.2 + 18 D.2.3 unit tests because the bot_config fixture is a bare MagicMock that auto-vivifies any attribute path and returns truthy. Fixed in two commits: 7c0e313 (2-line source fix in auth.py + server.py) and d39696a (5-file fixture + test realignment from `bot_config.api.*` → `bot_config.http_api.*`). After fix: tests/api/ back to 115 passed; droplet smokes A (REST Bearer → 200 + JSON), B (WS valid HS256 ticket → upgrade held with full register/unregister lifecycle log events), and C (WS bad ticket → CLOSE 4001 + ws_ticket_invalid log event) all green.

### Deferred

- Strategy-loop telemetry writing to BotState — deferred to D.2.4. Snapshot handler currently returns OQ-3 stub empties for openPositions, equityHistory, monthlyPnl, backtest, recentLogs, and recentAlerts. D.2.4 will also wire `emitter.broadcast(...)` into the strategy loop's fill / alert / strategy-update paths (D.2.3 set up the infrastructure but does not produce messages from real strategy execution).
- Permanent persistence of the operator-local `ports: - "127.0.0.1:8080:8080"` mapping in docker-compose.yml. Currently reapplied manually on the droplet after each `git pull` because the upstream compose file omits the block. D.2.4 will either commit a tracked `docker-compose.override.yml` or env-var-gate the `ports:` stanza via `BOT_HTTP_API_EXPOSE_PORT`.

### Operator notes

- HTTP/WebSocket API surface remains disabled by default (`BotConfig.http_api.enabled = False`). To enable on a droplet: set `BOT_HTTP_API_ENABLED=true`, `BOT_HTTP_API_BOT_API_TOKEN=<openssl rand -hex 32>`, `BOT_HTTP_API_WS_TICKET_SECRET=<openssl rand -hex 32>` in `.env` (mode 600); restart the container; bind defaults to `0.0.0.0:8080` inside the container — pair with a loopback `ports:` block in docker-compose.yml so only the droplet itself can reach 8080.

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
