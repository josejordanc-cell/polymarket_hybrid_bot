# `polymarket_hybrid_bot` — Documentation Security Review

**Version:** v2 — 2026-05-04
**Supersedes:** v1 (2026-05-03)

**Scope:** `README.md`, `HANDOFF.md`, `DEPLOY_DO.md`, `.env.example`, `.gitignore`, `docker-compose.yml`, `Dockerfile` (commit on `main` as of fetch time).
**Not yet reviewed (deferred to follow-up pass):** `requirements.txt`, `CHANGELOG.md`, `TESTING.md`, `TESTING_CHECKLIST.md`, contents of `/docs`, `.dockerignore`, inline code comments, `deploy/rebuild.sh`, `healthcheck.py`. These are itemised in §10.
**Methodology:** OWASP Top 10 (web + API), OWASP ASVS L1/L2 cherry-picked for a self-hosted trading agent, CIS Docker Benchmark v1.6 (selected), and standard secure-coding hygiene (least privilege, fail-closed defaults, defense-in-depth, secret management, dependency provenance, observability without leakage).

---

## What changed in v2

v1 flagged §1.1 (production droplet IP + root-deploy details committed to a public repo) as **CRITICAL** with a "do not merge anything else first" gate. A hardening session on 2026-05-04 closed the actual operational exposure: the repo is now private, the droplet is SSH key-only with fail2ban active, the kernel is current, and an audit of the exposure window shows no successful logins from outside the maintainer's residential ISP. §1.1 is now **MEDIUM (mitigated)** — the recommendation that survives is "no production identifiers in committed files going forward", not "rotate every secret you have ever owned".

The other findings still apply, in their original severities. This v2 also folds in two follow-up fixes from the same session:

- The bind-mount cache-permission warning (`market_cache_write_failed: Permission denied`) is silenced by a chown applied to `/app/` inside the running container. This is a runtime fix that will revert if the container is recreated; the durable fix is to move the cache to `/app/data/` in the bot code (v0.3.0 ticket).
- The `.env` inline-comment-after-value pattern that caused the v0.2.2 `equity_reconciliation_disabled: Non-hexadecimal digit found` incident has been swept on the production droplet — 14 instances rewritten to 0. The cleaned `.env` is on disk; the running container still parses the old version (Pydantic-Settings reads at process start) and will pick up the cleanup on the next restart.

The mitigation log in §0.1 below documents the verification commands and their observed output for each step.

---

## 0. Executive summary

Overall posture: **YELLOW**, no immediate gating action.

The repo's documentation is genuinely better than typical for a personal trading bot — it already calls out `chmod 600 .env`, recommends paid RPCs, has a non-root container user, hardens SSHD in writing, ships rotated logs, and pins dependency majors. After today's hardening session, the host-side posture matches what the docs prescribe. The findings below are mostly about closing remaining gaps in the *committed* artefacts (Dockerfile, compose, .gitignore, .env.example, SECURITY.md), not rewriting from scratch.

### Top findings, ranked

| # | Severity | Finding | Where | Section |
|---|---|---|---|---|
| 1 | ~~CRITICAL~~ → **MEDIUM (mitigated 2026-05-04)** | Production droplet IP + root-deploy details committed to public repo. Repo now private; host hardened; audit shows no compromise during exposure window. The going-forward rule is to keep production identifiers out of committed files. | `HANDOFF.md` §"v0.2.3 droplet verification" | §1.1, §3 |
| 2 | HIGH | Bot deploys as `root` on host; documented as known issue without a hard "do not flip to live" gate | `HANDOFF.md`, `DEPLOY_DO.md` | §1.2, §4 |
| 3 | HIGH | `.gitignore` doesn't ignore `data/`, `logs/`, `reports/`, wallet/key file globs — easy to accidentally commit fills, PnL, or a key dump | `.gitignore` | §6 |
| 4 | HIGH | No `SECURITY.md`, no vulnerability-disclosure path, no documented secret-rotation cadence | repo-wide | §1.3, §11 |
| 5 | HIGH | Equity reconciliation currently disabled (`BOT_POLYGON_RPC_URL` unset) — bot's PnL is not cross-checked against chain. Documentation treats this as benign; it isn't, for a live bot | `HANDOFF.md` | §3, §11 |
| 6 | MEDIUM | Container missing standard hardening: `read_only`, `cap_drop: [ALL]`, `no-new-privileges`, resource limits, dedicated network | `docker-compose.yml` | §7 |
| 7 | MEDIUM | Base image not pinned by digest; `requirements.txt` not hash-pinned (acknowledged as v0.3.0 open question — make it a v0.2.x security backport instead) | `Dockerfile`, HANDOFF | §8 |
| 8 | MEDIUM (RESOLVED) | ~~Metrics endpoint binds `0.0.0.0` by design~~ — **FIXED** 2026-05-04 by T1 (PR #9, see §12.1.1 row 1). Prometheus exporter now hardcodes `addr="127.0.0.1"` at `observability/metrics_exporter.py:161` (with explicit `127.0.0.1`-only banner in the `start()` docstring at `:154`). A stray `-p 9091:9091` no longer exposes the endpoint — the socket only listens on loopback. The exec-summary row was simply never updated to mirror §12.1.1. The "no auth on metrics" tail still stands as guidance for any future external exposure (`BOT_METRICS_AUTH_TOKEN` env field is reserved in `.env.example:87` but not yet implemented). | `observability/metrics_exporter.py:154,161`, `.env.example`, `docker-compose.yml` | §5 |
| 9 | MEDIUM | Telegram token, Discord webhook, Polygon RPC URL are documented as if they were configuration; they are secrets and should be labelled and handled as such | `.env.example` | §5 |
| 10 | MEDIUM | `.env` inline-comment-after-value pattern (root cause of the v0.2.2 incident) survived in the deployed `.env`. **Mitigated 2026-05-04** on the droplet (14 → 0); same fix needs to land in `.env.example` so new deploys don't reintroduce it | `.env`, `.env.example` | §5 |
| 11 | LOW (mitigated) | Bot persists market cache to `/app/.market_cache.json` (image-root path), not to a bind-mounted writable directory. Symptom: repeated `market_cache_write_failed` warnings on a non-root deploy. **Worked around 2026-05-04** by `chown bot:bot /app/` in the running container; durable fix is to move the cache file path to `/app/data/market_cache.json` in code **Mitigated 2026-05-04** by T10 (PR #14) — see §0.1.1. | code path | §4 |
| 12 | LOW | No structured guidance on log redaction (private keys, wallet addresses, JWTs, signed messages) — trading bots routinely leak these by accident in stack traces | repo-wide | §11 |
| 13 | LOW | No CI security checks documented (gitleaks, pip-audit, hadolint, trivy) | repo-wide | §11 |
| 14 | LOW (RESOLVED) | ~~No dashboard-integration auth model defined yet~~ — **SHIPPED** in v0.2.6+ HTTP API + v0.2.9 D.2 wave. `api/auth.py` implements `verify_bearer()` (REST `Authorization: Bearer <token>` per `api/auth.py:115`) and `verify_ws_ticket()` (WS HS256 JWT via `?ticket=<jwt>` per `api/auth.py:179,213`). Secrets land in `HttpApiConfig` at `config/settings.py:239` with env prefix **`BOT_HTTP_API_`** (real vars: `BOT_HTTP_API_BOT_API_TOKEN`, `BOT_HTTP_API_WS_TICKET_SECRET`, `BOT_HTTP_API_ENABLED`, `BOT_HTTP_API_BIND_HOST`, `BOT_HTTP_API_PORT`) — NOT `BOT_METRICS_*`. Endpoints gated: `GET /dashboard/snapshot`, `POST /api/strategies/config`, `POST /emergency-stop` (Bearer); `GET /ws` (HS256 JWT ticket, close `4001` on failure). Production hotfix `7c0e313` + `d39696a` realigned `config.api.*` → `config.http_api.*` after a 401 incident — be wary of the older `config.api.*` references that still appear in `docs/architecture/D.2.1-http-module.md` and a few source docstrings. | `api/auth.py`, `config/settings.py:239`, `README.md:15`, CHANGELOG `[Unreleased]` D.2.x | (resolved) |

---

## 0.1 Mitigation log — 2026-05-04 hardening session

This section documents what was actually done on the production droplet during the v1 → v2 cycle, with the verification commands and their observed output. It is the audit trail for the §1.1 downgrade.

### Pre-state (start of session)

- Repo: public, with `HANDOFF.md` containing `root@157.230.63.174` and `/opt/polymarket_hybrid_bot`.
- SSH: `PermitRootLogin yes`, `PasswordAuthentication yes`, no fail2ban, kernel `6.8.0-110-generic`, 16 pending updates including kernel.
- Brute-force traffic: ongoing, ~10 distinct attacker IPs visible in `journalctl -u ssh` over the prior 24 hours (sample: `20.193.153.215`, `147.182.194.60`, `8.130.88.141`, `2.57.121.25`, `213.209.159.56`, `45.148.10.121`).
- The only `Accepted` SSH login in the same window was from `190.235.10.234` (maintainer's Lima ISP).

### Actions taken (in order)

1. **Repo flipped to Private** (GitHub UI). Stops further passive crawling of the IP and deploy path. Does not retract anything that has already been mirrored, indexed, or scraped.

2. **SSH config rewritten** at `/etc/ssh/sshd_config.d/99-hardening.conf`:
   - `PermitRootLogin prohibit-password`
   - `PasswordAuthentication no`
   - `KbdInteractiveAuthentication no`
   - `ChallengeResponseAuthentication no`
   - `PubkeyAuthentication yes`

3. **Conflicting cloud-init drop-in neutralised.** `/etc/ssh/sshd_config.d/50-cloud-init.conf` originally re-enabled password auth (default cloud-init behaviour). Backed up and overwritten with `PasswordAuthentication no`. Cloud-init regen disabled by writing `/etc/cloud/cloud.cfg.d/99-disable-passwd-auth.cfg` containing `ssh_pwauth: false`. Verified live config:

   ```
   permitrootlogin without-password
   pubkeyauthentication yes
   passwordauthentication no
   kbdinteractiveauthentication no
   ```

4. **ED25519 key generated** on the maintainer's Mac with a passphrase stored in a password manager. Public key appended to `/root/.ssh/authorized_keys` on the droplet. Fingerprint:

   ```
   SHA256:k7I52aTNAf457TItYojWZ6p0uWjnnzkWHu8JmQePd7M  josejordan-mac-20260504  (ED25519)
   ```

   Three pre-existing DigitalOcean web-console ECDSA keys remained (expired, harmless, not removed in this pass). Authorized_keys mode confirmed `600`, `.ssh` mode `700`.

5. **fail2ban installed and enabled** with `/etc/fail2ban/jail.d/sshd.local`:

   ```ini
   [sshd]
   enabled  = true
   backend  = systemd
   maxretry = 5
   findtime = 10m
   bantime  = 1h
   ```

   Within seconds of starting, three brute-force IPs were already banned: `147.182.194.60`, `160.119.76.200`, `193.46.255.86`. After reboot the ban list reset (expected — bantime is 1h and fail2ban does not persist by default); the jail is active and reseating bans as new attempts come in.

6. **`apt upgrade`** applied 14 upgrades + 6 new packages. Notable: kernel `6.8.0-110-generic` → `6.8.0-111-generic`, Docker `29.4.1` → `29.4.2`, libcurl/curl, sed (security-only updates from `noble-security`). One Ubuntu Pro / ESM-only package (`python3-wheel`) deferred — paywall.

7. **`unattended-upgrades` + `apt-listchanges` installed** with `/etc/apt/apt.conf.d/20auto-upgrades` enabling daily security upgrades. Going forward, security patches land without manual intervention.

8. **Reboot.** Reconnected via key (passphrase prompt), confirmed kernel `6.8.0-111-generic` running, 0 updates pending, fail2ban active, bot container `Up 2 minutes (healthy)`. Strategy loop emitted `strategy_loop_started` with `markets: 46, mode: sim` at `2026-05-04T18:21:26Z`.

9. **Cache-permission warning silenced** by `docker exec -u root polymarket-hybrid-bot chown bot:bot /app/`. Reasoning: container runs as `uid=1000(bot)`, host bind mount at `/opt/polymarket_hybrid_bot` is already `1000:1000`, but the image ships `/app/` as `root:root` so the bot can't create files at the image-root path. Post-chown, no `market_cache_write_failed` events in a 3-minute log window. **This fix is ephemeral** — it does not survive container recreation. Tracked as a v0.3.0 ticket: change the bot to write its cache to `/app/data/market_cache.json` (which is a bind mount and properly owned). **Update 2026-05-04, post-tag:** the durable fix landed as T10 (PR #14, commit `82908ec`) — cache path moved to `/app/data/market_cache.json` (configurable via `BOT_MARKET_CACHE_PATH`), backed by a `market_cache_data` Docker named volume. The ephemeral chown workaround is no longer needed and the v0.3.0 ticket is closed.

10. **`.env` inline-comment cleanup.** Scanned `/opt/polymarket_hybrid_bot/.env` for the v0.2.2 footgun pattern (`KEY=VALUE   # comment`). Found 14 lines (`BOT_MODE`, `BOT_LOG_LEVEL`, `BOT_SIM_SLIPPAGE_BPS`, `BOT_METRICS_ENABLED`, `BOT_METRICS_PORT`, `BOT_POLYMARKET_PRIVATE_KEY`, `BOT_POLYMARKET_API_KEY`, `BOT_RISK_MAX_TRADE_PCT`, `BOT_RISK_DAILY_DRAWDOWN_LIMIT_PCT`, `BOT_RISK_KELLY_FRACTION`, `BOT_RISK_MIN_ORDER_USD`, `BOT_STRAT_SMART_MONEY_ENABLED`, `BOT_STRAT_MIN_ARB_EDGE`, `BOT_TELEGRAM_TOKEN`). Rewrote each to put the comment on its own line above the assignment. Backup at `.env.bak.before-comment-cleanup-20260504-184119`. Post-scan returned 0 hits. **The running container still parses the old, messy file** — Pydantic-Settings reads `.env` once at process start. Restart deferred until the next change-batch (this is just a `docker compose restart bot`, ~5-second outage).

### Post-state (end of session)

| Concern | Pre | Post |
|---|---|---|
| Repo visibility | Public | Private |
| SSH password auth | Enabled | Disabled |
| SSH root login | `yes` | `prohibit-password` (key-only) |
| fail2ban | Not installed | Active, banning brute-force IPs |
| Kernel | 6.8.0-110 with pending upgrade | 6.8.0-111, 0 pending |
| Auto-security-updates | Off | On (`unattended-upgrades`) |
| Bot health | Up, healthy | Up, healthy, post-reboot verified |
| Cache permission warning | Spam every cycle | Silent (ephemeral chown; v0.3.0 code fix planned) |
| `.env` inline-comment footgun | 14 instances | 0 instances on disk; running container picks up cleanup on next restart |

### Audit: was anything compromised during the exposure window?

The repo was public from approximately 2026-05-03T05:06Z (when the IP was first committed in `HANDOFF.md`) until 2026-05-04 (when it was flipped private). During that window:

- No `Accepted` SSH login from any IP other than `190.235.10.234` (maintainer's Lima ISP) appears in `journalctl -u ssh --since "7 days ago"`. The brute-force volume is high (hundreds of `Failed password`/`Invalid user` lines) but every one of them failed.
- The bot container's image (`polymarket_hybrid_bot:0.2.1`) was not modified — still building from local `Dockerfile`, no pull-from-registry path is configured.
- `.env` modification time is `May 3 16:56` (pre-session), and the file's checksum has not changed except where this session changed it deliberately.

This audit is the basis for declining the v1 "rotate every secret" recommendation. It is not the basis for declining all of §1's editorial changes — the going-forward rule (no production identifiers in committed files) still applies.

## 0.1.1 Mitigation log — 2026-05-04 post-tag patches

After the v0.2.4 release tag, twelve additional fixes were merged on
`main` and deployed to production (sim mode). Each closes a finding
flagged in this review.

| Finding | Patch | PR / commit | Status |
| --- | --- | --- | --- |
| Prometheus metrics exposed on all interfaces | `observability/metrics_exporter.py` now binds to `127.0.0.1` | PR #9, commit `0623eb4` | MITIGATED |
| `kelly_fraction` upper bound permits full-Kelly bankroll allocation | `BotRiskConfig.kelly_fraction` capped at `le=0.50` (floor `ge=0.05` preserved) | PR #10, commit `9af6254` | MITIGATED |
| `deploy/rebuild.sh` did not validate `.env` mode or presence | Fail-fast guards added: missing `.env` → exit 1 with remediation hint; non-600 mode → exit 1; dual `stat` fallback (Linux + BSD/macOS) | PR #11, commit `0eccb66` | MITIGATED |
| `BOT_BINANCE_SYMBOLS=[]` does not actually disable the Binance feed (pydantic-settings v2 ignores empty-list env values) | New `BOT_DISABLE_BINANCE_FEED` env flag in `data_feeds/spot_feed.py` (renamed from `binance_feed.py`, commit `c78ff69`; env-var and `binance_feed_disabled` log event preserved); emits `binance_feed_disabled` INFO log; in production, eliminated ~1500 `spot_feed_tick_error` events/hour from a US/DO IP | PR #12, commit `6a2e9ed` | MITIGATED |
| `.market_cache.json` write path is `/app/` (image root, read-only for `bot`); ephemeral chown applied 2026-05-04 was not durable across container recreation | New `BOT_MARKET_CACHE_PATH` (default `/app/data/market_cache.json`); auto-mkdir; Dockerfile creates `/app/data/` owned by `bot`; docker-compose mounts `market_cache_data` named volume so cache survives rebuilds | PR #14, commit `82908ec` | MITIGATED |
| Telegram alerts sent `parse_mode: "HTML"` on payloads mixing log lines and market symbols | Dropped `parse_mode` from the webhook JSON in `utils/alerts.py`; messages render as plain text; body shape otherwise unchanged | PR #17, commit `db21e18` | MITIGATED |
| `BOT_STRAT_SMART_MONEY_ENABLED=true` while wallet indexer `_scan_recent_wallets` is still a stub | `model_validator` on `StrategyConfig` refuses startup with pydantic `ValidationError` mentioning `_scan_recent_wallets` and stub; cross-reference comment in `data_feeds/polygon_feed.py` | PR #18, commit `87ccd71` | MITIGATED |
| Strategy loop could run silently for long stretches between order events | `main.py` emits `strategy_loop_started` once at loop entry and `strategy_loop_heartbeat` every 300 iterations (~60s at 0.20s interval) with `iterations`, `markets_evaluated_total`, `orders_submitted`; follow-up `7f7f934` adds `nonlocal _hb_orders_submitted` in nested `_exec_and_record` | PR #19, commits `415b04c` + `7f7f934` | MITIGATED |
| No structured per-feed liveness signal for stall detection | `spot_feed_heartbeat` and `polymarket_feed_heartbeat` every 60s (WARNING when stale > 30s) | PR #20, commit `5b89694` | MITIGATED |
| `healthcheck.py` cron healthcheck pings `https://api.binance.com/api/v3/ping`, which always returns HTTP 451 from the production droplet's US/DO IP — the check reports `degraded` regardless of bot state, making it a useless signal | Removed `BINANCE_PING_URL` constant and `check_binance_api()`; added `SPOT_FEED_HEARTBEAT_FRESHNESS_SEC = 120` and `check_spot_feed()` that scans `docker logs --since 3m` for `spot_feed_heartbeat` events (newest-first) and reports `ok` when the latest is < 120s old. Renames the check from `binance_api` to `spot_feed`. Disabled venues (T11) no longer trigger false-negative degraded states. Verified on droplet 2026-05-05: `last_heartbeat=20s ago venue=coinbase (limit=120s)` | PR #22, commit `a515728` | MITIGATED |
| Polymarket credentials stored as plain `str` in `ApiCredentialsConfig` — `polymarket_private_key`, `polymarket_api_key`, `polymarket_api_secret`, `polymarket_api_passphrase`, and `polygon_rpc_url` rendered in cleartext in `repr()` / `model_dump()` / structlog dumps, leaving them one accidental log line away from disclosure | Converted all five fields to `pydantic.SecretStr` in `config/settings.py`; twelve callsites updated to call `.get_secret_value()` when reading cleartext (4 in settings.py validator body, 4 in clob_executor.py ClobClient construction, 1 in polygon_feed.py RPC init, 1 in main.py:178 reconciliation loop, 1 in wallet_reconcile_cli.py:80, 1 in equity_reconciliation.py:64 conditional unwrap). SecretStr auto-redacts in `repr()`, `model_dump()`, and `__str__`. Production smoke test 2026-05-04: container rebuilt healthy, `strategy_loop_started` fires (markets: 48), `equity_reconciliation_disabled` correctly fires with reason `BOT_POLYGON_RPC_URL unset` proving the unwrap path works, `repr(config.api)` confirmed all 5 fields render as `SecretStr(...)` | PR #24, commit `0e3b268` (merge `7e783ef`) | MITIGATED |
| `correlation_blocked` (`risk_manager.py:190-213`) was a silent no-op — iterated `_state.open_positions` composite keys (`{market_id}_{outcome}`) against `_return_history` (keyed by bare `market_id`); every lookup returned `[]` and the `n < 20` early-out fired unconditionally, leaving non-arb strategies with zero correlation protection. | Iterate `self._state.open_positions.values()` and read `Position.market_id` (option c — zero new state, matches existing access pattern at `main.py:99,110`). Logger event `correlation_block` at lines 206-211 unchanged. **The guard is now actively evaluating; expect `correlation_block` log events in sim post-deploy — that is the guard finally working, not a regression.** Threshold tuning (`cfg.risk.max_pair_correlation`, default `0.80`) is a separate post-T5 concern before the live flip. Verification: 4 new unit tests in `tests/test_correlation_blocked.py`; strengthened `tests/test_record_return_correlation.py`; revert-proof smoking-gun (failed on revert, passed on staged); local pytest 24/24 + 7/7 targeted; droplet smoke 2026-05-08 confirmed healthy with all heartbeats firing, in-container 7/7 PASSED, bidirectional smoke `T5_SMOKE_OK`, 3-minute log scan zero tracebacks. | PR #28, commit `704ddcc` | MITIGATED |

Verification on the production droplet (157.230.63.174, `sim` mode):
the five fixes that were in the original post-tag batch (T1, T2, T6,
T11, T10) were confirmed working end-to-end via direct
`/proc/net/tcp` inspection (T1), startup boot test (T2), three
rebuild.sh guard paths (T6), a paired regression/kill-switch test
showing 25 `spot_feed_tick_error` events/min before vs. 0 after (T11),
and post-deploy mount/ownership/JSON-validity inspection plus a
follow-up rebuild proving cache persistence via `market_cache_hit` on
re-start (T10). Rows for T3, T4, T5, T7, T9, T12, T13, and T14 document subsequent merges
to `main` in the same mitigation-log shape; re-run smoke checks on the
next production roll.

A v0.2.5 release tag has not yet been cut. Tagging is deferred until
the remaining Phase 1 / Phase 2 hardening items in
`CURSOR_TASKS_v0.2.4.md` land (T3, T4, and T5 are now in; remaining items continue to roll on `main`), so
v0.2.5 can be a single coherent "post-tag hardening" release.

---

## 1. Action items

### 1.1 Production droplet exposure — MITIGATED 2026-05-04

`HANDOFF.md`, in the section "v0.2.3 droplet verification (2026-05-03)", commits the production droplet's IP and confirms `root` deploys at a known path. This is no longer an active risk — see §0.1 — but the editorial fix remains worthwhile as defense-in-depth.

**What still needs to happen (low-priority editorial):**

1. Redact the IP and deploy path from `HANDOFF.md` HEAD (replace with `<droplet-ip>` / `<deploy-path>` placeholders). Diff in §3 below. This is cosmetic — the IP is already in git history and possibly in scraper caches — but it stops further casual exposure.
2. Going-forward rule: **no production IPs, hostnames, deploy paths, or usernames in committed files.** Use placeholders and keep concrete values in your password manager / 1Password / `ops/` private repo. This rule is captured in `HANDOFF.md` §"Maintainer secret-handling rules" (added by §3 below).
3. *Optional* git history rewrite (`git filter-repo --replace-text expressions.txt`) to redact the IP from history. Cosmetic at this point and breaks any clones/forks; only worth doing if you have no collaborators and want a clean public history if you ever flip the repo back to public.

**What v1 said that is now superseded:**

- v1 recommended rotating `BOT_POLYMARKET_PRIVATE_KEY`, `BOT_POLYMARKET_API_*`, `BOT_POLYGON_RPC_URL`, the Telegram bot token, and the Discord webhook on grounds that the host might have been compromised. The audit in §0.1 shows it was not. **Do not rotate prophylactically** — key rotation for the EOA is operationally heavy (requires fund migration), and rotation without cause adds risk (a fresh key with a typo is worse than a known-good key on a hardened host). Rotate on suspicion of actual exposure, per the cadence in `docs/SECRET_MANAGEMENT.md` (skeleton in §11.2).

### 1.2 Add a hard gate against running `live` mode while deployed as root

Unchanged from v1. This is a documentation-and-code fix; the doc side is in §3 and §4 below. The code side — preferably in `config/` settings — should refuse to boot when `BOT_MODE=live` and the process is running as UID 0. That's outside Task 1, but the docs should explicitly call it out as a v0.2.4 task and refuse to recommend a `live` flip until it ships.

### 1.3 Add `SECURITY.md` and a private disclosure path

Unchanged from v1. A repo without `SECURITY.md` invites public issue-tracker disclosures of vulnerabilities even if the repo is currently private — the rule is "set this up before you ever flip back to public, and even when private it gives external researchers a clear channel". Full content in §11.1 below.

---

## 2. Cross-cutting recommendations (apply across all docs)

These thread through the file-by-file revisions in §3–§9. Unchanged from v1 except where noted.

**Secrets terminology.** Anywhere a doc currently treats Telegram tokens, Discord webhooks, RPC URLs (with API keys), CLOB API key/secret/passphrase, or the EOA private key as "configuration", relabel them as **secrets** and route them through the same handling rules (`.env` with `chmod 600`, never logged, rotated on a documented cadence, never echoed in error messages or stack traces, never embedded in URLs that show up in referrer headers or proxy logs).

**Fail-closed defaults.** Every doc should reinforce that `BOT_MODE=sim` is the safe default, and that any new feature defaults to off. The current `.env.example` does this for the smart-money strategy; extend the principle to risk parameters (no default `BOT_RISK_BANKROLL_USD=10000` on a fresh install — require it to be set explicitly).

**Doc–reality drift.** `DEPLOY_DO.md` describes a non-root deploy procedure the actual deploy doesn't follow. Either fix the deploy or rewrite the doc to describe the *current* (root) state and label the non-root path "target state, not current state". Right now both docs describe both, which makes reviewers think one of them is fiction. *Update for v2:* the SSH/host-hardening side of `DEPLOY_DO.md` is now no longer drift — fail2ban, key-only SSH, unattended-upgrades, kernel-current are all real on the droplet. The non-root container deploy is still drift.

**Reproducibility.** Pin the Docker base image by digest, hash-pin `requirements.txt` (pip-tools `--generate-hashes`), and check both into version control. HANDOFF lists this as a v0.3.0 open question; for a bot that signs trades worth real money, treat it as a security backport for the next 0.2.x release.

**Observability without leakage.** Every log line that interpolates user-controlled or chain-controlled data (market IDs, wallet addresses, error messages from third-party clients) should pass through a redaction filter that strips:
- Anything matching `0x[a-fA-F0-9]{64}` (private keys)
- Anything matching `eyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}` (JWTs)
- The configured wallet address (replace with `<wallet>` for non-debug levels)
- The configured RPC URL (replace with `<rpc>`)

This is a code change but the docs should specify the requirement so it's enforced in code review.

**Input validation surface.** Document every external input source: CLOB REST/WS responses, Polygon RPC responses, Binance/Coinbase ccxt responses, env vars at boot, weekly-report CLI args. For each, specify schema validation (pydantic models, ideally) and the failure mode if validation fails (skip and log? abort the cycle? kill the process?). The `BOT_POLYGON_WALLET_ADDRESS=` "Non-hexadecimal digit found" issue from v0.2.2 was an env-var validation gap with a soft failure; **the proximate cause was the inline-comment pattern, swept on the droplet on 2026-05-04 (§0.1) but still present in `.env.example`** — the underlying failure mode (silent disable instead of hard boot error) is unfixed, and per-field pydantic validators are the right structural fix.

---

## 3. `HANDOFF.md` — UPDATED (redact + restructure)

**Why:** removes the production IP, removes the root-user disclosure, removes ambiguous "running as root is acceptable" framing, adds explicit gating for `live` mode, adds verification fingerprints rather than free-text claims. *No longer CRITICAL gating per v2 — but still the most valuable single editorial change.*

```diff
@@  v0.2.3 droplet verification (2026-05-03)  @@
-The chown quick-fix from `DEPLOY_DO.md` has been applied on the production
-droplet (`root@157.230.63.174`, `/opt/polymarket_hybrid_bot`):
-
-    chown -R 1000:1000 /opt/polymarket_hybrid_bot
-    docker compose up -d   # no-op for compose; bind mount picks up new ownership in the running container
-
-Result, verified at 2026-05-03T05:06Z:
+The chown quick-fix from `DEPLOY_DO.md` has been applied on the production
+droplet (host details in private ops vault, not committed). The fix:
+
+    chown -R 1000:1000 <deploy-path>
+    docker compose up -d   # required to recreate the container so the bind mount
+                           # picks up the new ownership; `restart` is not enough
+
+Result, verified at 2026-05-03T05:06Z (sim mode):
```

```diff
@@  Open issues for v0.2.3  @@
-These three are config/ownership issues, not code bugs. They don't block
-v0.2.2 acceptance but should be fixed before the bot moves out of sim mode.
+These three are config/ownership issues, not code bugs. They are HARD
+blockers for `BOT_MODE=live` — the bot must not flip to live until all
+three are resolved AND the host is no longer running the container as root.
```

Append the new section below at the end of `HANDOFF.md`:

```markdown
UPDATED: appended ──────────────────────────────────────────────────────────

## Live-mode gating checklist (do not merge `BOT_MODE=live` without these)

- [ ] `BOT_POLYGON_RPC_URL` set to a paid provider (not a public RPC) and
      reconciliation has produced at least one successful run with drift below
      `BOT_RECONCILIATION_DRIFT_THRESHOLD_PCT` for 24 consecutive hours in
      sim mode.
- [ ] `BOT_POLYMARKET_PRIVATE_KEY` is fresh, has never been committed, has
      never been pasted into chat, and is the only key with USDC funding.
- [ ] Host runs the container as a non-root user (`bot`, UID 1000) per the
      "Proper fix" in `DEPLOY_DO.md`. Quick-fix chown is acceptable for sim
      but not for live.
- [ ] `chmod 600 .env` verified by `stat -c '%a' .env` returning `600`.
- [ ] `.env` contains no inline `KEY=VALUE  # comment` lines (the v0.2.2
      footgun). Verify with `grep -nE '^\s*[A-Z_][A-Z0-9_]*=.*#' .env`
      returning empty.
- [ ] `discord_alert_failed` and `equity_reconciliation_disabled` are absent
      from the last 24h of logs (`docker compose logs bot --since 24h | \
      grep -E 'discord_alert_failed|equity_reconciliation_disabled'` returns
      no rows).
- [ ] Risk limits in `.env` reviewed and signed off:
      `BOT_RISK_BANKROLL_USD`, `BOT_RISK_MAX_TRADE_PCT`,
      `BOT_RISK_DAILY_DRAWDOWN_LIMIT_PCT`, `BOT_RISK_KELLY_FRACTION`.
- [ ] Kill-switch path tested in sim: trip the daily drawdown limit
      synthetically, verify the bot stops opening new positions and emits
      a structured `risk_killswitch_tripped` event.
- [ ] Backup of `data/trades.jsonl` and `.env` (offline, encrypted) taken
      within the last 7 days.

## Maintainer secret-handling rules

1. No production IPs, hostnames, deploy paths, or usernames in committed
   files. Use `<droplet-ip>` / `<deploy-path>` placeholders.
2. No secrets (CLOB API key/secret/passphrase, EOA private key, RPC URL,
   Telegram token, Discord webhook) in any file under version control,
   including this one. `.env.example` shows variable NAMES only.
3. Secrets live in the host's `.env` (mode 600, owner = run-user) or in a
   secret store (sops + age, HashiCorp Vault, 1Password CLI). They are NEVER
   pasted into chat, screenshots, issue comments, PR descriptions, or any
   AI assistant context window — including this one.
4. Rotation cadence: CLOB API creds every 90 days; EOA on suspicion of any
   exposure (no scheduled rotation because rotation requires fund migration
   and is operationally heavy — keep the EOA tightly scoped instead).
5. On any suspicion of compromise, follow `docs/INCIDENT_RESPONSE.md`.
```

---

## 4. `DEPLOY_DO.md` — UPDATED

**Why:** the current doc tells the reader to harden SSHD and run as a non-root user, then a section below admits production runs as root. Reorganise so the current state is unambiguous, hardening is checklist-form, and DigitalOcean Cloud Firewall is added as a defense-in-depth layer above UFW. *Update for v2:* the SSHD / fail2ban / unattended-upgrades / current-kernel side is now real on the production droplet (per §0.1) — the doc should describe the current state, not the aspirational state.

```diff
@@  Initial server setup  @@
 1. Create a non-root user (e.g. `bot`) with `sudo`.
 2. Harden **sshd** (`PermitRootLogin no`, `PasswordAuthentication no`, deploy SSH keys).
 3. Install **Docker Engine** and **Compose v2** from Docker's official Ubuntu repository (not deprecated `docker-compose` 1.x).
 4. **UFW:** allow only **OpenSSH** (`ufw allow OpenSSH && ufw enable`).
+5. **DigitalOcean Cloud Firewall** (defense-in-depth, applied at the hypervisor):
+   - Inbound: TCP/22 from your management CIDR(s) only. No `0.0.0.0/0:22`.
+   - Outbound: allow `tcp/443` (CLOB, Polygon RPC, Telegram, Discord, ccxt
+     exchanges); deny everything else.
+6. Enable **automatic security updates**: `apt install unattended-upgrades`,
+   `dpkg-reconfigure --priority=low unattended-upgrades`.
+7. Install and enable **fail2ban**: `apt install fail2ban && systemctl enable
+   --now fail2ban`. Add `/etc/fail2ban/jail.d/sshd.local` with
+   `maxretry=5`, `findtime=10m`, `bantime=1h`.
+8. Set up **off-host log shipping** (DigitalOcean Monitoring, Loki, or
+   similar). Local logs only is a single point of failure; if the droplet
+   is compromised, the only audit trail goes with it.
```

```diff
@@  Application install  @@
-    sudo -u bot -i
-    git clone <repo> polymarket_hybrid_bot && cd polymarket_hybrid_bot
-    cp .env.example .env
-    chmod 600 .env
-    # Fill BOT_POLYMARKET_* for live; leave BOT_MODE=sim for smoke test.
-    ./deploy/rebuild.sh
+    # MUST be the `bot` user, not root. If you cloned as root, see
+    # "Running as root on the droplet" below for migration steps.
+    sudo -u bot -i
+    git clone <repo> polymarket_hybrid_bot && cd polymarket_hybrid_bot
+    cp .env.example .env
+    chmod 600 .env
+    # Fill secrets. BOT_MODE=sim until the live-mode checklist in
+    # HANDOFF.md is fully green. Never edit .env in an editor that
+    # creates swap files in a sync'd folder.
+    stat -c '%a %U' .env   # MUST print "600 bot"
+    grep -nE '^\s*[A-Z_][A-Z0-9_]*=.*#' .env   # MUST return empty
+    ./deploy/rebuild.sh
```

Replace the "Running as root on the droplet (current state)" section wholesale:

```markdown
UPDATED: replacement for "Running as root on the droplet (current state)" ─

## Running as root on the droplet — known issue, not a recommendation

Until v0.2.4 the production droplet ran the deploy tree as `root` at
`/opt/<deploy-path>`. This was tolerated for **sim mode only** and is not
acceptable for `BOT_MODE=live`. The exact symptom is:

    {"error": "[Errno 13] Permission denied: '.market_cache.json'",
     "event": "market_cache_write_failed", "level": "warning"}

The bot inside the container runs as UID 1000 (`bot`); a root-owned bind
mount blocks writes. As of 2026-05-04 a workaround chown (`docker exec -u root
<container> chown bot:bot /app/`) silences the warning at runtime but does
not survive container recreation. The durable fix is to migrate to a
non-root deploy AND move the cache write path to `/app/data/` in code
(tracked for v0.3.0).

### Migration to non-root deploy (required before `live`)

1. Take a fresh backup of `data/` and `.env`.
2. Create a non-root user with the same UID the container expects:

       adduser --uid 1000 --disabled-password --gecos "" bot
       usermod -aG docker bot

3. Re-clone under `~bot`:

       su - bot -c 'git clone <repo> ~bot/polymarket_hybrid_bot'

4. Restore `.env` and `data/` from backup, set ownership and modes:

       chown -R bot:bot ~bot/polymarket_hybrid_bot
       chmod 600 ~bot/polymarket_hybrid_bot/.env

5. Stop the old container, start from the new tree:

       cd /opt/<old-path> && docker compose down
       cd ~bot/polymarket_hybrid_bot && ./deploy/rebuild.sh

6. Verify: `ps -eo user,cmd | grep docker` shows `bot` as the docker client
   invoker, and inside the container `id` returns `uid=1000(bot)`.
7. Remove the old root-owned tree only after a clean run:
   `rm -rf /opt/<old-path>` (only after step 6 is green).

### Quick-fix chown (sim mode only)

If you cannot do the migration immediately and you are running sim mode:

    chown -R 1000:1000 <deploy-path>
    docker compose up -d
    docker exec -u root <container> chown bot:bot /app/

This silences `market_cache_write_failed` but does NOT make root deploys
acceptable for live trading. The second `chown` is ephemeral — it must be
re-applied if the container is recreated, until the v0.3.0 code change
moves the cache to `/app/data/`.
```

Append a new "Update / patching" section:

```markdown
UPDATED: appended ──────────────────────────────────────────────────────────

## Update and patching cadence

| Layer | Cadence | How |
|---|---|---|
| Host kernel + base packages | weekly + on CVE | `unattended-upgrades` for security; manual reboot window for kernel |
| Docker engine | monthly | apt upgrade; verify `docker info` after |
| Bot image | per release tag | `git pull && ./deploy/rebuild.sh --no-tail`, observe logs for 60 min |
| Python deps (advisory) | weekly | `pip-audit -r requirements.txt` from a workstation; rebuild image on findings |
| Base image digest | with each `pip-audit` rebuild | bump `Dockerfile` `FROM ...@sha256:` tag |

## Backups

`data/trades.jsonl`, `data/reports/`, and `.env` should be backed up at
least weekly to off-host, encrypted storage (DigitalOcean Spaces with
server-side encryption, B2, or restic over SSH). `.env` requires
client-side encryption (`gpg -c` or sops) regardless of the destination's
SSE — a 600-mode .env on the droplet is one snapshot away from being
world-readable in the backup target.
```

---

## 5. `.env.example` — UPDATED

**Why:** relabel secrets as secrets, add metrics-endpoint binding control, add a placeholder for future dashboard auth, strengthen the warning blocks, and remove ambiguity introduced by the v0.2.2 inline-comment incident. *Update for v2:* the inline-comment pattern was swept from the *production `.env`* on 2026-05-04 (14 → 0). The fix needs to land in `.env.example` so new deploys don't reintroduce it — that's exactly what the replacement file below does (every comment is on its own line, never trailing a value).

`UPDATED:` full replacement file:

```bash
# =============================================================================
# polymarket_hybrid_bot — environment variable template
# -----------------------------------------------------------------------------
# Copy to .env, fill in secrets, then `chmod 600 .env` (verify with
# `stat -c '%a %U' .env` -> "600 <run-user>"). Never commit .env, never paste
# its contents into chat, screenshots, issue comments, PR descriptions, or
# AI-assistant context windows.
#
# IMPORTANT: do NOT add inline comments after a value (e.g. `KEY=value # note`).
# The env loader reads the comment as part of the value and will silently
# break the variable (this is what caused
# `equity_reconciliation_disabled: Non-hexadecimal digit found` in v0.2.2).
# Use block comments above the variable instead. Verify before deploy with:
#   grep -nE '^\s*[A-Z_][A-Z0-9_]*=.*#' .env
# (the command must return empty).
#
# Variables marked [SECRET] must NEVER appear in:
#   - committed files (including this one — only the variable NAME is here)
#   - logs (the bot's logger redacts known patterns; do not bypass)
#   - error messages or stack traces (PRs that print these are blocked)
#   - external monitoring / APM payloads
#
# Production secret-store alternatives to a plaintext .env:
#   - sops + age (file-level encryption, plays well with git for non-secret
#     keys and encrypted values); see docs/SECRET_MANAGEMENT.md
#   - HashiCorp Vault with the AppRole auth method
#   - 1Password CLI / op-cli for ad-hoc retrieval at deploy time
# =============================================================================

# --- Runtime mode ----------------------------------------------------------

# Bot operating mode. Always start new deploys in 'sim' (paper trading) and
# only flip to 'live' after the live-mode checklist in HANDOFF.md is fully
# green. Valid: sim | live
BOT_MODE=sim

# Identifier for A/B experiments. Use 'default' for production runs and
# 'arm_a' / 'arm_b' / etc. when running parallel strategy variants.
BOT_EXPERIMENT_ID=default

# Log verbosity. Valid: DEBUG | INFO | WARNING | ERROR
# DEBUG must NEVER be used in live mode -- it can leak signed payloads.
BOT_LOG_LEVEL=INFO

# How often the main bot loop polls (seconds, can be fractional).
BOT_POLL_INTERVAL_SEC=0.25

# How often the strategy layer evaluates (seconds, can be fractional).
BOT_STRATEGY_INTERVAL_SEC=0.20

# Paper-trading slippage applied in sim mode, in basis points.
# 5 bps = 0.05%. Ignored in live mode.
BOT_SIM_SLIPPAGE_BPS=5

# --- Order-fill polling ----------------------------------------------------

# How often to poll for fill confirmations (seconds).
BOT_FILL_POLL_INTERVAL_SEC=2

# Give up waiting for a fill after this many seconds.
BOT_FILL_POLL_TIMEOUT_SEC=60

# How often to check whether resolved markets have been settled.
BOT_RESOLUTION_POLL_SECONDS=60

# --- Metrics / observability -----------------------------------------------

# Whether to expose Prometheus metrics over HTTP. 'false' disables the
# background HTTP server entirely.
BOT_METRICS_ENABLED=true

# Bind address for the metrics HTTP server INSIDE the container.
# Default 127.0.0.1 means metrics are only reachable from inside the
# container (e.g. via `docker compose exec` or a sidecar exporter).
# Use 0.0.0.0 ONLY if you intend to publish the port via compose AND have
# put authentication in front of it (reverse proxy with basic auth, mTLS,
# or BOT_METRICS_AUTH_TOKEN once implemented).
BOT_METRICS_BIND_HOST=127.0.0.1

# Port for the metrics HTTP server.
BOT_METRICS_PORT=9091

# [SECRET] Bearer token required on /metrics. Empty disables auth (only safe
# when BOT_METRICS_BIND_HOST=127.0.0.1). Required when binding to 0.0.0.0
# or when the dashboard integration begins. Generate with:
#   python -c "import secrets; print(secrets.token_urlsafe(48))"
BOT_METRICS_AUTH_TOKEN=

# --- Equity reconciliation -------------------------------------------------

# How often to cross-check internal PnL against on-chain wallet balance.
# Requires BOT_POLYGON_WALLET_ADDRESS and BOT_POLYGON_RPC_URL to be set.
# A bot in live mode without working reconciliation is FLYING BLIND on
# whether its books match reality. This is a hard live-mode gate.
BOT_RECONCILIATION_INTERVAL_SECONDS=3600

# Alert threshold for drift between internal and on-chain equity, in percent.
BOT_RECONCILIATION_DRIFT_THRESHOLD_PCT=1.0

# Wallet address used for equity reconciliation (the funder address).
# Leave empty in sim mode. Required when BOT_MODE=live or when you want
# on-chain reconciliation. Must be a 0x-prefixed 40-char hex address.
# This is NOT a secret but it IS attributable on-chain — anyone with this
# value can see your trade history.
BOT_POLYGON_WALLET_ADDRESS=

# --- Polymarket CLOB API ---------------------------------------------------

# Polymarket CLOB REST host. Default is correct for production.
BOT_POLYMARKET_HOST=https://clob.polymarket.com

# Polymarket CLOB WebSocket URL. Currently returns 404 — REST polling
# fallback is active. Tracked for v0.3.0.
BOT_POLYMARKET_WS_URL=wss://ws-subscriptions-clob.polymarket.com/ws/

# [SECRET] EOA private key for signing CLOB orders.
# Generate fresh, fund only the operating bankroll, never reuse for
# personal use, never commit, never share, never paste into chat.
# 0x-prefixed 64-char hex string.
BOT_POLYMARKET_PRIVATE_KEY=

# [SECRET] Polymarket CLOB API credentials (L2 auth). Generate via the
# Polymarket CLOB API derivation flow — see docs/SECRET_MANAGEMENT.md.
# Rotate every 90 days or on any compromise suspicion.
BOT_POLYMARKET_API_KEY=
BOT_POLYMARKET_API_SECRET=
BOT_POLYMARKET_API_PASSPHRASE=

# --- Reference price feeds -------------------------------------------------

# Spot symbols polled from Binance for reference pricing.
# JSON-array string. Set BOT_DISABLE_BINANCE_FEED=true to skip Binance entirely
# (e.g. when running from a US/DO IP that gets HTTP 451 responses).
BOT_BINANCE_SYMBOLS=["BTC/USDT","ETH/USDT","SOL/USDT"]

# Spot symbols polled from Coinbase for reference pricing.
BOT_COINBASE_SYMBOLS=["BTC/USD","ETH/USD","SOL/USD"]

# --- Polygon RPC -----------------------------------------------------------

# [SECRET] Polygon mainnet RPC URL. Use a paid provider (QuickNode, Alchemy,
# Infura) — public RPCs rate-limit and are not safe for live trading.
# Treat the URL as a SECRET because it almost always contains your provider
# API key in the path or query string.
BOT_POLYGON_RPC_URL=

# Polygon chain ID. 137 = mainnet. Do not change unless deploying to a
# testnet (Mumbai = 80001).
BOT_CHAIN_ID=137

# --- Risk management -------------------------------------------------------

# Starting bankroll in USD. Used as the denominator for all percentage
# risk limits below. NO DEFAULT — set explicitly per deploy.
BOT_RISK_BANKROLL_USD=

# Max position size per trade as a fraction of bankroll. 0.08 = 8%.
# Recommended range: 0.05–0.10.
BOT_RISK_MAX_TRADE_PCT=0.08

# Daily kill-switch: pause trading if cumulative loss exceeds this fraction
# of bankroll. 0.12 = 12%.
BOT_RISK_DAILY_DRAWDOWN_LIMIT_PCT=0.12

# Kelly-criterion fraction. 0.25 = quarter-Kelly (conservative).
# Range: 0.10 (very cautious) to 1.0 (full Kelly, aggressive).
BOT_RISK_KELLY_FRACTION=0.25

# Max correlation allowed between any two open positions, to limit
# concentration risk. 0.80 = 80%.
BOT_RISK_MAX_PAIR_CORRELATION=0.80

# Maximum number of simultaneously open positions.
BOT_RISK_MAX_OPEN_POSITIONS=8

# Minimum order size in USD. Orders below this are skipped as dust.
BOT_RISK_MIN_ORDER_USD=10.0

# --- Strategy toggles & parameters -----------------------------------------

# Layer 1 — momentum strategy.
BOT_STRAT_MOMENTUM_ENABLED=true

# Layer 2 — passive market-making strategy.
BOT_STRAT_MARKET_MAKER_ENABLED=true

# Layer 4 — smart-money copy strategy. Off by default; requires whale
# tracking infrastructure.
BOT_STRAT_SMART_MONEY_ENABLED=false

# Smart-money: minimum historical win rate of a wallet before copying.
BOT_STRAT_SMART_MONEY_MIN_WIN_RATE=0.60

# Smart-money: minimum profit factor of a wallet before copying.
BOT_STRAT_SMART_MONEY_MIN_PROFIT_FACTOR=1.50

# Skip markets with on-CLOB liquidity below this USD threshold.
BOT_STRAT_MIN_LIQUIDITY_USD=5000

# Minimum guaranteed edge after fees before placing an arb. 0.005 = 50 bps.
BOT_STRAT_MIN_ARB_EDGE=0.005

# Momentum: z-score threshold for signal triggering.
BOT_STRAT_MOMENTUM_Z_THRESHOLD=2.0

# Momentum: rolling window length in number of ticks.
BOT_STRAT_MOMENTUM_WINDOW=60

# Market-maker: half-spread to quote on each side, in basis points.
# 40 bps = 0.40% on each side, 0.80% total spread.
BOT_STRAT_MM_SPREAD_BPS=40

# --- Alerting --------------------------------------------------------------

# [SECRET] Telegram bot token (from @BotFather). Anyone with this token can
# send messages as the bot. Leave empty to disable Telegram alerts.
BOT_TELEGRAM_TOKEN=

# Telegram chat or channel ID to send alerts to. Required if
# BOT_TELEGRAM_TOKEN is set.
BOT_TELEGRAM_CHAT_ID=

# [SECRET] Discord channel webhook URL. Anyone with this URL can post to
# the channel as the bot. Treat as a secret. Empty disables Discord alerts.
# Format: https://discord.com/api/webhooks/<id>/<token>
BOT_DISCORD_WEBHOOK_URL=

# --- Feature flags ---------------------------------------------------------

# Skip Binance (HTTP 451 from many US/DO regions). Set to true to disable.
BOT_DISABLE_BINANCE_FEED=false

# Suppress repeated WS 404 warnings from the Polymarket WS upgrade until
# v0.3.0 ships a stable WS URL.
SUPPRESS_WS_404_WARNINGS=
```

---

## 6. `.gitignore` — UPDATED

**Why:** the current file ignores `.env*`, `*.key`, `*.pem`, and `*.log` but leaves `data/`, `logs/`, `reports/`, and any wallet/key dump file pattern uncovered. A trading bot accumulating fills in `data/trades.jsonl` is a very-not-public file once it's been live.

`UPDATED:` full replacement:

```gitignore
# Python
__pycache__/
*.py[cod]
*$py.class
*.so
.Python
.pytest_cache/
.coverage
htmlcov/
.tox/
.mypy_cache/
.ruff_cache/

# Virtual environments
venv/
env/
.venv/

# OS
.DS_Store
Thumbs.db

# IDE
.vscode/
.idea/
*.swp
*.swo

# Secrets, keys, and runtime state
.env
.env.local
.env.*.local
.env.bak.*
*.key
*.pem
*.p12
*.pfx
secrets/
keys/
wallet*.json
mnemonic*
private_key*
*.kdbx

# Bot runtime artifacts
.market_cache.json
data/
!data/.gitkeep
logs/
!logs/.gitkeep
reports/
!reports/.gitkeep
bot.log
*.log

# Backups and temp
*.bak
*~
*.orig
*.rej

# Docker
.docker/
```

(Add empty `data/.gitkeep`, `logs/.gitkeep`, `reports/.gitkeep` files so the directories exist post-clone but their contents are never tracked.)

---

## 7. `docker-compose.yml` — UPDATED

**Why:** the current compose ships a non-root container user (good — set in the Dockerfile) and rotates logs (good), but is missing CIS Docker Benchmark essentials: `read_only` filesystem with explicit tmpfs, dropped capabilities, `no-new-privileges`, resource limits, and a dedicated bridge network.

`UPDATED:` full replacement:

```yaml
services:
  bot:
    build:
      context: .
      dockerfile: Dockerfile
    image: polymarket_hybrid_bot:0.2.4
    container_name: polymarket-hybrid-bot
    restart: unless-stopped

    # User is set in the Dockerfile but pin it here as defense-in-depth in
    # case someone swaps in a different image tag.
    user: "1000:1000"

    env_file:
      - .env

    volumes:
      - ./logs:/app/logs
      - ./data:/app/data
      - ./scripts:/app/scripts:ro

    # Read-only root filesystem; the bot writes only to mounted volumes
    # and to /tmp (tmpfs below) for the heartbeat file used by healthcheck.
    read_only: true
    tmpfs:
      - /tmp:rw,nosuid,nodev,size=64m

    security_opt:
      - no-new-privileges:true

    cap_drop:
      - ALL
    # Add capabilities back here ONLY if a specific dependency requires
    # them. None are currently needed.

    # Resource ceilings — prevents a runaway loop or memory leak from
    # taking the host down. Tune for the actual droplet size.
    mem_limit: 1g
    mem_reservation: 256m
    cpus: 1.5
    pids_limit: 256

    # Dedicated bridge network — no implicit access to other compose
    # projects on the same host.
    networks:
      - bot_net

    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "5"

    healthcheck:
      test:
        [
          "CMD-SHELL",
          "test -f /tmp/.bot_alive && test $$(($$(date +%s) - $$(stat -c %Y /tmp/.bot_alive))) -lt 90 || exit 1",
        ]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 120s

networks:
  bot_net:
    driver: bridge
    driver_opts:
      com.docker.network.bridge.name: br-pmbot
```

**Note on `read_only`:** if any code path writes to e.g. `/app/.cache/`, `/home/bot/.cache/`, or similar, it will fail. The bot already writes its market cache via the `data/` bind mount per the v0.2.3 fix — *the 2026-05-04 session worked around this with an ephemeral chown, and T10 (PR #14, commit `82908ec`) shipped the durable fix moving the cache to `/app/data/market_cache.json`*. This compose change is now safe to ship — the cache path dependency was resolved in T10. **The remaining concern is that any OTHER code path writing under `/app/` (outside `/app/data/`, `/app/logs/`, `/app/scripts/`, `/tmp`) will fail under `read_only: true`; review before merging.**

If you want the rest of the compose hardening (cap_drop, no-new-privs, limits, dedicated network) before the cache-path code change, drop just `read_only: true` and `tmpfs` from this replacement; the others have no such dependency.

---

## 8. `Dockerfile` — UPDATED

**Why:** pin base image by digest, add OCI labels for traceability, add hash-pinning advisory, add `tini` for clean signal handling so the bot stops cleanly on `docker compose down` (which matters because partial shutdowns can leave open orders).

`UPDATED:` full replacement:

```dockerfile
# syntax=docker/dockerfile:1
# polymarket_hybrid_bot — multi-stage runtime (v0.2.4)

# ---- Builder ---------------------------------------------------------------
# Pin by digest to make the build reproducible and prevent supply-chain
# tampering via `latest`-style tag mutation. Bump intentionally on each
# security advisory rebuild — see DEPLOY_DO.md "Update and patching cadence".
FROM python:3.11-slim-bookworm@sha256:REPLACE_WITH_PINNED_DIGEST AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        gcc \
        libffi-dev \
        libssl-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt .
# When requirements.txt is migrated to hash-pinned form (pip-compile
# --generate-hashes), add `--require-hashes` here. Tracked in HANDOFF.md.
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip setuptools wheel \
    && /opt/venv/bin/pip install -r requirements.txt

# ---- Runtime ---------------------------------------------------------------
FROM python:3.11-slim-bookworm@sha256:REPLACE_WITH_PINNED_DIGEST AS runtime

LABEL org.opencontainers.image.title="polymarket_hybrid_bot" \
      org.opencontainers.image.source="https://github.com/<owner>/polymarket_hybrid_bot" \
      org.opencontainers.image.licenses="<TBD>" \
      org.opencontainers.image.version="0.2.4"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PATH="/opt/venv/bin:${PATH}"

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        tzdata \
        tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 bot \
    && useradd --uid 1000 --gid bot --shell /usr/sbin/nologin --create-home bot

COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
COPY --chown=bot:bot . /app

USER bot

# tini is PID 1 — it reaps zombies and forwards SIGTERM to the bot,
# allowing the asyncio shutdown handler to cancel open orders cleanly.
ENTRYPOINT ["/usr/bin/tini", "--"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
    CMD test -f /tmp/.bot_alive \
    && test "$(($(date +%s) - $(stat -c %Y /tmp/.bot_alive 2>/dev/null || echo 0)))" -lt 90 \
    || exit 1

CMD ["python", "-m", "polymarket_hybrid_bot.main"]
```

**Action item:** before merging this Dockerfile change, run `docker pull python:3.11-slim-bookworm` and copy the resulting digest from `docker inspect` into the `@sha256:` placeholder. Repeat on each rebuild and record the digest in `CHANGELOG.md`.

**Side note from §0.1:** the `COPY --chown=bot:bot . /app` line above fixes the v2 finding #11 (the cache-permission warning) at *build time* — meaning the runtime `chown` workaround applied on 2026-05-04 becomes unnecessary once this Dockerfile ships. The bot will still benefit from also moving the cache file to `/app/data/` (so it survives image rebuilds and lives on the bind-mount), but `--chown=bot:bot` is the correct image-side fix regardless.

---

## 9. `README.md` — UPDATED

> ⚠️ **STALE — DO NOT APPLY (2026-05-27 audit annotation).** The full-file replacement below is a v0.2.4-era proposal that was never shipped. The repo has since moved through the v0.2.6 (HTTP API), v0.2.8 (D.2 dashboard wiring), and v0.2.9 wave (Phase 3 wire-shape alignment + snapshot history wiring + derived-stats + Sharpe/Sortino) — multiple `## Status (v0.2.4)` rows below are no longer accurate (e.g. `Container hardens` / `Non-root deploy on host` state; the legacy `DISABLE_BINANCE_FEED=1` flag — real var is `BOT_DISABLE_BINANCE_FEED`; the `docs/THREAT_MODEL.md` reference points at a never-created file per §11). Applying this section verbatim would regress the current `README.md`. The section is retained as a historical record of the v2-audit recommendation; treat the current `README.md` as canonical. A fresh README-modernization pass would need to be scoped against `[Unreleased]` reality, not this proposal.

**Why (original v2 audit framing, preserved):** add Security and Threat Model entry points, point to the live-mode checklist, replace the loose "v0.2.1 status" with a clearly-labeled stability matrix, and remove ambiguous status icons in favour of explicit pass/fail/known-issue language.

`UPDATED:` full replacement:

```markdown
# polymarket_hybrid_bot

Async Polymarket hybrid strategies (sim default): feeds → risk → priority strategy loop → CLOB execution.

> ⚠️ **Trading software, real money.** Default mode is `sim` (paper). Do not flip to `live` until the live-mode checklist in `HANDOFF.md` is fully green and `docs/THREAT_MODEL.md` has been reviewed for your deployment. The author(s) accept no liability for losses.

## Status (v0.2.4)

| Area | State | Notes |
|---|---|---|
| Docker build | OK | `web3` 7.x + explicit `hexbytes` pin resolves `eth-account===0.13.0` conflict with `py-clob-client==0.22.0` |
| Strategy loop | OK | Look for `strategy_loop_started` after boot |
| REST (Polymarket / Gamma) | OK | Primary data path |
| WebSocket (Polymarket CLOB) | DEGRADED | Upgrade handshake returns 404; bot falls back to REST polling. `SUPPRESS_WS_404_WARNINGS=1` quiets the noise |
| Binance spot feed | BLOCKED in many regions | HTTP 451 from US / most DO POPs; set `BOT_DISABLE_BINANCE_FEED=true` and use EU/SG regions for ccxt live |
| Equity reconciliation | OFF until `BOT_POLYGON_RPC_URL` set | Hard live-mode gate |
| Container hardens (read-only fs, dropped caps, no-new-privs) | ON | See `docker-compose.yml` |
| Non-root deploy on host | TARGET STATE — see `DEPLOY_DO.md` migration steps | Hard live-mode gate |

## Quick start (sim mode, on your workstation)

```bash
git clone <your-fork-url> polymarket_hybrid_bot
cd polymarket_hybrid_bot
cp .env.example .env
chmod 600 .env
# Leave BOT_MODE=sim. Fill BOT_POLYGON_WALLET_ADDRESS only if you have one;
# everything else can stay empty for a sim smoke test.
./deploy/rebuild.sh
```

For production deploy on DigitalOcean, follow `DEPLOY_DO.md` end-to-end —
every step is a defense-in-depth layer, not optional.

## Working build commands

Equivalent to `./deploy/rebuild.sh` minus the readiness poll:

```bash
docker compose down --remove-orphans
docker compose build --pull
docker compose up -d
docker compose logs -f --tail=50 bot
```

`./deploy/rebuild.sh` flags: `--no-tail` (exit after readiness check),
`--nuke` (prune image/build cache before build).

## Project layout

```
polymarket_hybrid_bot/
├── main.py                    # Async runtime entry (sim/live)
├── polymarket_hybrid_bot/     # Package shim: python -m polymarket_hybrid_bot.main
├── config/                    # Pydantic settings (BOT_* env)
├── data_feeds/                # Polymarket, Binance/Coinbase, Polygon
├── strategies/                # Arb, momentum, MM, smart-money
├── execution/                 # CLOB executor + sim
├── deploy/
│   └── rebuild.sh             # Idempotent compose rebuild + readiness poll
├── scripts/
│   ├── weekly_report.py       # trades.jsonl weekly aggregate
│   └── wallet_reconcile.py
├── data/                      # Mounted volume — gitignored, off-host backups
├── logs/                      # Mounted volume — gitignored
├── Dockerfile                 # Multi-stage builder → slim runtime, digest-pinned
├── docker-compose.yml         # Service `bot`, hardened (read-only, cap_drop)
├── requirements.txt           # Pinned deps (hash-pinning tracked for v0.2.5)
├── HANDOFF.md                 # Maintainer handoff and live-mode checklist
├── DEPLOY_DO.md               # DigitalOcean droplet guide
├── SECURITY.md                # Vulnerability disclosure policy
└── docs/
    ├── ARCHITECTURE.md        # System diagram + data flow
    ├── THREAT_MODEL.md        # STRIDE per component
    ├── SECRET_MANAGEMENT.md   # How to handle and rotate keys
    └── INCIDENT_RESPONSE.md   # Detect → contain → eradicate → recover
```

## Configuration

See `.env.example` — every variable is documented inline, secrets are tagged
`[SECRET]`, and the file format gotchas (no inline comments after values)
are spelled out. **Never edit `.env` in an editor that auto-syncs to a cloud folder.**

## Known issues

1. **Polymarket WebSocket 404** — REST polling fallback. Tracked for v0.3.0.
2. **Binance 451** — Geo / compliance block. Use `fra1` / `sgp1` / `lon1`,
   or `BOT_DISABLE_BINANCE_FEED=true`. Tracked for v0.3.0.

## Operations cheatsheet

| Task | Command |
|---|---|
| Follow logs | `docker compose logs -f bot` |
| Tail last hour, errors only | `docker compose logs --since 1h bot \| grep -iE 'error\|exception\|traceback'` |
| Weekly report | `docker compose exec -T bot python -m scripts.weekly_report --weeks 1 --stdout` |
| Restart stack | `./deploy/rebuild.sh --no-tail` |
| Stop | `docker compose down` |
| Update | `git pull && ./deploy/rebuild.sh` |
| Verify .env permissions | `stat -c '%a %U' .env` (must print `600 <run-user>`) |
| Verify .env has no inline comments | `grep -nE '^\s*[A-Z_][A-Z0-9_]*=.*#' .env` (must return empty) |
| Verify container is non-root | `docker compose exec bot id` (must print `uid=1000(bot)`) |

## Security

- **Reporting a vulnerability:** see `SECURITY.md`. Do not file public issues for security bugs.
- **Threat model:** see `docs/THREAT_MODEL.md`.
- **Secret handling:** see `docs/SECRET_MANAGEMENT.md`.
- **Incident response:** see `docs/INCIDENT_RESPONSE.md`.

## More documentation

- `HANDOFF.md` — Maintainer triage + live-mode checklist
- `DEPLOY_DO.md` — DigitalOcean sizing, hardening, cron, backups, patching
- `CHANGELOG.md` — Release notes (include base-image digest per release)
- `docs/` — Architecture, threat model, secret management, IR

## License

<TBD — add an explicit LICENSE file. No license = "all rights reserved", which is fine for a personal repo but blocks even informal collaboration.>
```

---

## 10. Files I have not yet reviewed

> **Status update (2026-05-27):** several items in the table below have been REVIEWED and/or RESOLVED in subsequent rounds; the "Status" column annotates each. The original (v2) framing is preserved verbatim in the "Why review matters" column so the audit history reads intact. **Do not read this table as an open audit gap** — it's a mix of resolved, in-progress, and still-open items. Verify against §12.1 / §12.1.1 / `CHANGELOG.md [Unreleased]` / `.swarm-decisions.md` before treating any row as actionable.

| File | Why review matters | Status |
|---|---|---|
| `requirements.txt` | Verify version pinning, plan migration to `pip-compile --generate-hashes`, run `pip-audit` and triage findings | **DONE.** `requirements.txt` is generated from `requirements.in` via `pip-compile --allow-unsafe --generate-hashes` (verify the header at `requirements.txt:1-5`); pip auto-enforces hash-checking. See `HANDOFF.md:69` (root) for the canonical statement. `pip-audit` runs are operator-side cadence, not a code gate. |
| `.dockerignore` | Confirm `.env`, `data/`, `logs/`, `.git`, `tests/`, `*.md` are excluded so secrets and noise don't enter the build context | Still owed (small targeted review). |
| `Dockerfile` (line 32 area) | Confirm no curl-pipe-bash, no `ADD <url>`, no installer scripts (already looks clean from what I've seen) | Still owed (small targeted review). |
| `deploy/rebuild.sh` | Confirm `set -euo pipefail`, no unsafe `eval`, no `curl \| bash`, idempotent failures | Still owed (small targeted review). |
| `healthcheck.py` | Confirm it doesn't open a network port without auth, doesn't print env, fails closed | Partially addressed by T11 (PR #22) — `healthcheck.py` now uses spot-feed heartbeat instead of network ping; further surface review still owed. |
| `config/` (Pydantic settings module) | Confirm validators exist for hex addresses, URL fields, numeric ranges (so the v0.2.2 inline-comment failure mode becomes a hard boot error, not a silent disable) | **DONE 2026-05-05** as T13 / item #13 in §12.1.1 (PR #26, commit `352f8d0`) — ten pydantic validators added to `config/settings.py` covering URL schemes, hex addresses, key/secret lengths, JSON-array shape, and experiment-id alphabet. |
| `execution/` (CLOB executor) | Confirm it uses `verify=True` on every HTTP client, has explicit timeouts, retries with backoff, validates CLOB responses against a pydantic schema, and never logs signed payloads | Partially addressed by T12 (PR #24) — Polymarket credentials now `SecretStr` everywhere, auto-redact in `repr()` / `model_dump()` / structlog. Remaining items (verify=True, timeouts, response schema, signed-payload-log audit) still owed. |
| `data_feeds/` | Same checks as execution/ | Same status as `execution/` above. |
| `risk_manager.py` | Confirm hard limits cannot be silently bypassed (e.g. NaN bankroll → unbounded position size); confirm kill-switch state is durable across restarts (writes to `data/`, not in-memory only) | **PARTIALLY ADDRESSED.** Correlation-guard silent-no-op fixed 2026-05-08 as T14 (PR #28, commit `704ddcc`) — see §12.1.1 row 14 and the row in §0.1.1. `risk_manager.py` is the project's fenced authority surface per CLAUDE.md hard rule 2; line-level audit-trail is maintained in CLAUDE.md + `.swarm-decisions.md` (rows referencing `risk_manager.py:NN` are bounds-checked against the live file). NaN-bankroll and kill-switch-durability checks are still owed as a targeted review. |
| `scripts/wallet_reconcile.py` | Confirm read-only path, never signs anything, never prints private keys | Indirectly covered by T12 (PR #24) — `wallet_reconcile_cli.py:80` now calls `.get_secret_value()` on `polygon_rpc_url`, ensuring the cleartext is never accidentally logged. Read-only-path / no-signing checks still owed. |
| `scripts/weekly_report.py` | Confirm it doesn't ingest user-supplied paths without validation | **REVIEWED.** Wholesale rewrite at `554e2a8` (2026-05-02) replaced log-driven FIFO model with `trades.jsonl`-driven flat-aggregate. Current CLI accepts only `--weeks N` / `--format {md,json}` / `--stdout` / `-v` — no user-supplied paths exposed. Re-audited in the v0.2.9 wave (Sharpe/Sortino addition at `9f951e5`); side-effect-free-on-import preserved. |
| `tests/` | Confirm no real keys / wallets in fixtures; ideally add tests for the input-validation boundaries above | **REVIEWED (clean).** `tests/fixtures/sample_trades.jsonl` and `sample_logs.jsonl` use placeholder market_ids (`m1`/`m2`) and zero-balance values; conftest sets `BOT_MODE=sim` / `BOT_EXPERIMENT_ID=test` via `os.environ.setdefault` (`tests/conftest.py:133-134`) and stubs `py_clob_client` for hermetic runs. Validator tests landed with T13 (PR #26). |
| `TESTING.md`, `TESTING_CHECKLIST.md` | Align with the live-mode checklist; add explicit "test-key only, never live-key" rule | Stale-CLI sweep completed (commits `7bb3748`, `1bf18ec`) — current CLIs match real argparse. Explicit "test-key only" rule still owed as a separate edit. |
| `CHANGELOG.md` | Align with semver discipline; record base-image digest and dependency-audit summary per release | **MAINTAINED.** `[Unreleased]` is actively updated per wave (v0.2.9 + derived-stats + Sharpe/Sortino + B.7/B.8/B.9 + 3a–3i + Phase E.1 entries verified in this audit cycle). Base-image-digest-per-release discipline is operator-side cadence. |
| `docs/` (entire folder) | Read and either fold into the new docs/ structure or update in place | **PARTIALLY REVIEWED.** `docs/HANDOFF.md`, `docs/NEXT_STEPS.md`, `docs/VALIDATION_CHECKLIST.md`, `docs/FINAL_CODE_REVIEW.md`, `docs/architecture/D.2.1-http-module.md` audited and partially updated in the 2026-05-26 / 2026-05-27 drift sweeps. This file itself (`docs/SECURITY_REVIEW.md`) audited 2026-05-27. `docs/audit/d24-emit-site-audit.md` not yet audited. |
| Inline code comments | A separate pass; flag any TODOs that mention secrets, security, or "fix me later" | Still owed. |

---

## 11. New files to add

### 11.1 `SECURITY.md` (new)

```markdown
# Security policy

## Supported versions

Only the most recent minor release receives security fixes. Older versions
are out of scope.

| Version | Supported |
|---|---|
| 0.2.x | ✅ |
| 0.1.x and earlier | ❌ |

## Reporting a vulnerability

**Do not file public issues for security bugs.**

Report privately to: `<set-up-a-dedicated-email-or-github-private-vuln-reporting>`.
Include:
- A description of the vulnerability and the affected component (data feed,
  execution, risk manager, etc.).
- Reproduction steps or proof-of-concept.
- The version / commit hash you tested against.
- Your assessment of severity and exploitability.
- Whether you have already disclosed to anyone else.

You will receive an acknowledgement within 72 hours and a substantive
response within 7 days. Coordinated disclosure target: 90 days from report,
shorter if active exploitation is observed.

## Out of scope

- Bugs that require already-compromised host access (root on the droplet,
  read access to `.env`) — those are operational issues, not vulnerabilities.
- Findings against deprecated versions.
- Polymarket / Polygon / upstream library bugs — please report those upstream.
- Theoretical issues with no demonstrated path to harm.

## In scope

- Anything that lets an unauthenticated party place, cancel, or modify orders.
- Anything that lets an unauthenticated party read trading state, fills, PnL,
  or wallet identifiers.
- Anything that lets an authenticated dashboard user escalate beyond their
  documented scope (once the dashboard ships).
- Secret-leak vectors in logs, error messages, metrics endpoints, telegram
  alerts, or discord webhooks.
- Supply-chain issues (typosquats, hijacked dependencies, compromised
  base-image digests).
- Container-escape vectors specific to this image.
```

### 11.2 `docs/SECRET_MANAGEMENT.md` (new — skeleton, fill from your ops practice)

```markdown
# Secret management

## What counts as a secret in this repo

[from .env.example]: BOT_POLYMARKET_PRIVATE_KEY, BOT_POLYMARKET_API_KEY,
BOT_POLYMARKET_API_SECRET, BOT_POLYMARKET_API_PASSPHRASE,
BOT_POLYGON_RPC_URL (when it embeds an API key, which is almost always),
BOT_TELEGRAM_TOKEN, BOT_DISCORD_WEBHOOK_URL, BOT_METRICS_AUTH_TOKEN
(once implemented).

## Layout

[Document where secrets live in your environment: 1Password vault name,
sops file path, Vault mount path, etc.]

## Rotation cadence

| Secret | Cadence | Owner | Last rotated |
|---|---|---|---|
| BOT_POLYMARKET_PRIVATE_KEY | On suspicion of exposure only | <name> | <date> |
| BOT_POLYMARKET_API_* | 90 days | <name> | <date> |
| BOT_POLYGON_RPC_URL | 90 days | <name> | <date> |
| BOT_TELEGRAM_TOKEN | 180 days or on team change | <name> | <date> |
| BOT_DISCORD_WEBHOOK_URL | 180 days or on team change | <name> | <date> |
| BOT_METRICS_AUTH_TOKEN | 90 days | <name> | <date> |

## Rotation procedures

[Step-by-step for each: how to generate, how to deploy, how to confirm,
how to revoke the old one.]

## Emergency revocation

[Phone-tree style: who has the keys, who can press the kill-switch, what
order to revoke things in, who to notify.]
```

### 11.3 `docs/INCIDENT_RESPONSE.md` (new — skeleton)

```markdown
# Incident response

## Triggers

- Unexpected fills or positions visible in `data/trades.jsonl` that the
  strategy logs do not explain.
- `equity_reconciliation_drift` event with drift > threshold.
- Unexplained outbound connections from the host (check
  `journalctl -u docker | grep -iE 'pull|connect'`).
- SSH `Failed password` rate spike, `Accepted` from unknown IP, `lastb`
  showing brute force.
- A secret value appearing in any log file (search for the exact 12-char
  fingerprint of each secret with `grep -r <fingerprint> logs/`).
- GitHub Secret Scanning alert.
- Any alert from the dashboard (once it ships) you didn't expect.

## Phase 1 — Contain (first 5 minutes)

1. Trip the kill-switch: set `BOT_MODE=sim` in `.env` and `./deploy/rebuild.sh --no-tail`.
2. If you cannot reach the host: revoke `BOT_POLYMARKET_API_*` via the
   Polymarket dashboard so existing creds can no longer place orders.
3. Snapshot `data/`, `logs/`, and `.env` (encrypted) for forensics.

## Phase 2 — Eradicate

[Per-vector procedures: leaked private key, leaked API creds, host compromise,
dependency compromise, etc.]

## Phase 3 — Recover

- Generate fresh secrets per `SECRET_MANAGEMENT.md`.
- Re-deploy from a known-good commit.
- Re-fund a fresh EOA if the old one is burnt.

## Phase 4 — Lessons

- Append a post-mortem to `docs/postmortems/<date>-<title>.md`.
- File a corresponding fix in the issue tracker, even if low priority.
- Update this runbook with anything that surprised you.
```

### 11.4 `docs/THREAT_MODEL.md` (new — skeleton, fill collaboratively)

```markdown
# Threat model

## Assets

- Trading capital (USDC, MATIC) on the operating EOA.
- The EOA private key.
- CLOB API credentials.
- Polygon RPC API key.
- Trade history and PnL (privacy + competitive value).
- Strategy parameters (competitive value).

## Trust boundaries

[Diagram: bot ↔ Polymarket CLOB, bot ↔ Polygon RPC, bot ↔ Binance/Coinbase,
bot ↔ Telegram/Discord, host ↔ internet, future: bot ↔ dashboard.]

## STRIDE per component

[For each component: Spoofing, Tampering, Repudiation, Information disclosure,
Denial of service, Elevation of privilege. List threat → mitigation → status.]

## Out-of-scope assumptions

[Things you are assuming will not happen, e.g. "we trust the maintainer's
laptop is not compromised". List them so the assumption is explicit and
reviewable.]
```

### 11.5 `CONTRIBUTING.md` (new — security-relevant excerpt)

````markdown
# Contributing — security checks

## Required local checks before PR

```bash
# Secrets
gitleaks detect --source . --no-banner

# Python deps
pip-audit -r requirements.txt

# Lint + type
ruff check .
mypy .

# Static security
bandit -q -r polymarket_hybrid_bot/ strategies/ execution/ data_feeds/

# Container
hadolint Dockerfile
docker run --rm -v "$PWD:/repo" aquasec/trivy fs --severity HIGH,CRITICAL /repo
```

## CI (GitHub Actions) should run all of the above on every PR.

[Add the workflow file in `.github/workflows/security.yml` — see template.]

## What blocks a merge

- Any `gitleaks` finding (no exceptions; even test fixtures should not
  contain real-shaped secrets).
- Any `pip-audit` HIGH or CRITICAL with no documented mitigation.
- Any `bandit` HIGH severity not explicitly waived in the PR description.
- Any `trivy` HIGH or CRITICAL OS-level CVE without a base-image bump
  scheduled.
````

---

## 12. Remediation matrix

### 12.1 Completed during the 2026-05-04 hardening session

| # | Action | Severity | Status |
|---|---|---|---|
| C1 | Repo flipped to Private | (mitigation for §1.1) | ✅ done |
| C2 | SSH password auth disabled, root login set to `prohibit-password`, ED25519 key deployed and verified | HIGH | ✅ done |
| C3 | fail2ban installed, jail configured, banning brute-force IPs | HIGH | ✅ done |
| C4 | Cloud-init drop-in neutralised (no longer regenerates password auth) | MEDIUM | ✅ done |
| C5 | `apt upgrade` applied (kernel + 14 pkgs), reboot, post-reboot smoke test green | HIGH | ✅ done |
| C6 | `unattended-upgrades` + `apt-listchanges` installed and enabled | MEDIUM | ✅ done |
| C7 | `.market_cache.json` permission warning silenced via `docker exec chown` | LOW | ✅ done (ephemeral — see O8 below) |
| C8 | `.env` inline-comment cleanup on the production droplet (14 → 0) | MEDIUM | ✅ done on disk; takes effect on next container restart |
| C9 | Audit of exposure window — no successful logins from outside maintainer ISP | (basis for §1.1 downgrade) | ✅ done |

### 12.1.1 Completed post-v0.2.4 (T1, T2, T3, T4, T5, T6, T7, T8, T9, T10, T11, T12, T13, T14)

| # | Item | Type | Effort | Owner | Status |
| --- | --- | --- | --- | --- | --- |
| 1 | Bind Prometheus exporter to 127.0.0.1 | code | S | maintainer | DONE 2026-05-04 (PR #9) |
| 2 | Cap `kelly_fraction` at 0.50 | code | S | maintainer | DONE 2026-05-04 (PR #10) |
| 3 | rebuild.sh fail-fast on missing/world-readable `.env` | code | S | maintainer | DONE 2026-05-04 (PR #11) |
| 4 | `BOT_DISABLE_BINANCE_FEED` env flag | code | S | maintainer | DONE 2026-05-04 (PR #12) |
| 5 | Drop Telegram `parse_mode: "HTML"` | code | S | maintainer | DONE 2026-05-04 (PR #17) |
| 6 | Delete dead PascalCase strategies + deprecated `deploy/` duplicates | hygiene | S | maintainer | DONE 2026-05-04 (PR #15) |
| 7 | Smart-money strategy guard (refuse to load while indexer is a stub) | code | S | maintainer | DONE 2026-05-04 (PR #18) |
| 8 | Move market cache to `/app/data/market_cache.json` (durable, configurable) | code | S | maintainer | DONE 2026-05-04 (PR #14) |
| 9 | Strategy loop heartbeat (`strategy_loop_heartbeat` every ~60s) | code | S | maintainer | DONE 2026-05-04 (PR #19) |
| 10 | Per-feed liveness heartbeats (`spot_feed_heartbeat`, `polymarket_feed_heartbeat`) | code | S | maintainer | DONE 2026-05-04 (PR #20) |
| 11 | Replace misleading Binance API ping in `healthcheck.py` with `spot_feed` heartbeat-based liveness check (renames check from `binance_api` to `spot_feed`) | code | S | maintainer | DONE 2026-05-05 (PR #22) |
| 12 | Credential field type hardening (HIGH): convert four Polymarket credential fields and `polygon_rpc_url` from `str` to `pydantic.SecretStr` in `config/settings.py`. Twelve callsites updated to `.get_secret_value()` (4 in settings.py validator, 4 in clob_executor ClobClient construction, 1 in polygon_feed RPC init, 1 in main.py:178 reconciliation loop, 1 in wallet_reconcile_cli.py:80, 1 in equity_reconciliation.py:64 conditional unwrap; signature unchanged). SecretStr auto-redacts in `repr()`, `model_dump()`, `__str__`. Production smoke test 2026-05-04: container rebuilt healthy, `strategy_loop_started` (markets: 48), `repr(config.api)` shows all 5 fields as `SecretStr(...)`, zero credential strings in logs | code | S | maintainer | DONE 2026-05-04 (PR #24, `0e3b268` -> `7e783ef`) |
| 13 | Format validators for env vars (HIGH): add ten pydantic validators in `config/settings.py` so malformed env vars produce hard boot errors instead of silent failures. ApiConfig field validators on `polymarket_host` (https://), `polymarket_ws_url` (wss://), `polygon_rpc_url` (http(s):// when non-empty), `polymarket_private_key` (64 hex chars), `polymarket_api_key` (>=16 chars), `polymarket_api_secret` (>=16 chars), `polymarket_api_passphrase` (non-empty when set). ApiConfig model_validator on `binance_symbols`/`coinbase_symbols` (BASE/QUOTE pattern). BotConfig field validators on `polygon_wallet_address` (40 hex chars after 0x) and `experiment_id` (alphanumeric+underscore+hyphen). Empty-value short-circuits preserve sim mode (no creds set). Existing T3 `_live_mode_requires_keys` model_validator continues to enforce non-emptiness for live mode. Production smoke test 2026-05-04: container rebuilt healthy, `strategy_loop_started` (markets: 48), heartbeats firing, negative test `BOT_POLYMARKET_WS_URL=http://wrong-scheme.example.com` produced expected ValidationError mentioning `wss://`. Test fixture in `tests/test_live_fill_reconciliation.py::live_env` updated in same PR (BOT_POLYMARKET_API_KEY and BOT_POLYMARKET_API_SECRET bumped to 32 chars). | code | M | maintainer | DONE 2026-05-05 (PR #26, `9935ce5` -> `352f8d0`) |
| 14 | Correlation guard fix (HIGH): convert `correlation_blocked` in `risk_manager.py:190-213` from silent no-op to active evaluation. The guard iterated `_state.open_positions` composite keys (`{market_id}_{outcome}`) against `_return_history` (bare `market_id`), so every lookup returned `[]` and the `n < 20` early-out fired unconditionally — non-arb strategies bypassed correlation entirely. Fix (option c): iterate `self._state.open_positions.values()` and read `Position.market_id` from `Position`. Zero new state, zero parsing, zero sync risk. Matches existing access pattern at `main.py:99,110`. Logger event `correlation_block` at lines 206-211 unchanged. **The guard is now actively evaluating; expect `correlation_block` log events in sim post-deploy — that is the guard finally working, not a regression.** Threshold tuning (`cfg.risk.max_pair_correlation`, default `0.80`) is a separate post-T5 concern before the live flip. Tests: 4 new unit tests in `tests/test_correlation_blocked.py` (correlated→True, uncorrelated→False, n<20→False, self-market→False; seeded numpy); strengthened `tests/test_record_return_correlation.py::test_approve_orders_reaches_correlation_guard` (asserts `approved == []`). Revert-proof smoking-gun test failed on clean revert with `assert False is True`, passed on staged code. Local pytest 24/24 in full suite, 7/7 targeted. Droplet smoke 2026-05-08: container rebuilt to `polymarket_hybrid_bot:0.2.4`, Up (healthy) >2 min, all 3 heartbeats firing on cadence; in-container `pytest tests/test_correlation_blocked.py tests/test_record_return_correlation.py` 7/7 PASSED in 0.73s; bidirectional smoke `T5_SMOKE_OK`; 3-minute log scan zero tracebacks. | code | S | maintainer | DONE 2026-05-08 (PR #28, commit `704ddcc`) |

### 12.2 Still open, ranked by severity × tractability

| Rank | Action | Severity | Effort | Owner area |
|---|---|---|---|---|
| 1 | §3 — apply HANDOFF.md redactions (`<droplet-ip>`, `<deploy-path>` placeholders) and append live-mode checklist | MEDIUM | S | docs |
| 2 | §6 — apply `.gitignore` updates and add `.gitkeep` files | HIGH | S | docs/repo |
| 3 | §11.1 — add `SECURITY.md` with a private disclosure path | HIGH | S | docs |
| 4 | §1.2 — block `BOT_MODE=live` while running as root in code | HIGH | S | code |
| 5 | §5 — `.env.example` updates (relabel secrets, add metrics binding/auth, remove default bankroll, sweep inline-comment pattern) | HIGH | S | docs/code |
| 6 | §4 — `DEPLOY_DO.md` rewrite (Cloud FW, fail2ban procedure, off-host logs, backups, patching cadence — most of this is already real on the host, just needs to be the doc of record) | HIGH | M | ops/docs |
| 7 | Migrate the production deploy from root to non-root `bot` user (procedure already in revised `DEPLOY_DO.md`) | HIGH | M | ops |
| 8 ✅ DONE | Move `.market_cache.json` write path to `/app/data/market_cache.json` in code (durable replacement for C7); also fixes the `read_only: true` blocker in §7 **Done 2026-05-04 as T10 (PR #14, commit `82908ec`).** | MEDIUM | S | code |
| 9 | §7 — docker-compose hardening (cap_drop, no-new-privs, limits, dedicated network now; add `read_only` + tmpfs after item 8 ships) | MEDIUM | S | docs/code |
| 10 | §8 — Dockerfile digest pin + tini + OCI labels + `COPY --chown=bot:bot` | MEDIUM | S | docs/code |
| 11 | Container restart on the production droplet to pick up the cleaned `.env` (~5-second outage, batch with PR for items 1-5) | LOW | S | ops |
| 12 | Re-enable equity reconciliation (set `BOT_POLYGON_RPC_URL` and verify drift stays in tolerance) | MEDIUM | S | ops |
| 13 | §9 — README.md replacement | MEDIUM | S | docs |
| 14 | §11.2–§11.4 — fill in `SECRET_MANAGEMENT.md`, `INCIDENT_RESPONSE.md`, `THREAT_MODEL.md` skeletons | MEDIUM | M | docs |
| 15 | Hash-pin `requirements.txt` via `pip-compile --generate-hashes` and rebuild image with `--require-hashes` | MEDIUM | S | code |
| 16 | Add structured log redaction for known secret patterns; codify "no DEBUG in live" policy | MEDIUM | M | code |
| 17 | §11.5 — `CONTRIBUTING.md` with required local checks; add corresponding GitHub Actions workflow | MEDIUM | M | repo |
| 18 ✅ DONE | Add explicit pydantic validators for every env var (range checks, hex-address checks, URL scheme checks) so v0.2.2-style silent disables become hard boot errors **Done 2026-05-05 as T4 (PR #26, commit `352f8d0`).** | LOW | M | code |
| 19 | DigitalOcean Cloud Firewall for TCP/22 from maintainer ISP only (defense-in-depth above SSHD + UFW + fail2ban) | LOW | S | ops |
| 20 | Follow-up pass: review the §10 files I haven't seen yet | LOW | M | docs |
| 21 | Add `LICENSE` (the README mentions it as TBD) | LOW | S | repo |

---

## 13. What to merge first

If you want a single, low-risk first PR that captures the highest-value editorial changes without touching code:

- §3 — HANDOFF.md redaction + live-mode checklist
- §6 — `.gitignore` + `.gitkeep` files
- §11.1 — `SECURITY.md`
- §5 — `.env.example` (relabel + metrics binding + inline-comment warning + comment hygiene)

This PR is pure docs/config; no behaviour change. Container restart after this PR picks up the cleaned `.env` from §0.1 step 10 in the same outage.

Then a second PR for the operational changes:

- §7 — `docker-compose.yml` hardening (everything except `read_only` until item 8 in the matrix above ships)
- §8 — `Dockerfile` digest pin + tini + `COPY --chown=bot:bot`
- §4 — `DEPLOY_DO.md` rewrite
- §9 — `README.md` replacement

Then PRs per item in §12.2 from rank 8 onward.

This split keeps each PR small enough to review carefully, which matters because every one of these changes touches a system that signs trades for real money.
