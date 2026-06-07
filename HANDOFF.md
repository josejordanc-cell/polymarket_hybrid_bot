# HANDOFF — polymarket_hybrid_bot

## TL;DR

```bash
git pull && ./deploy/rebuild.sh
```

First-time on a fresh host: `cp .env.example .env`, fill secrets, `chmod 600 .env`, then `./deploy/rebuild.sh`.

## Current authoritative state (2026-06-07)

**Code:** `main` HEAD = `57d8b18`; **480 tests green** (local + CI run `27096324537`).
`risk_manager.py` is FENCED (read-only) — the `7255010` lift below was a single-change
exception and is now closed.

**Droplet:** `root@157.230.63.174:/opt/polymarket_hybrid_bot`, deploy lane `main`,
deployed at `57d8b18` (image `sha256:89676eb4…`), healthy. Rollback ref
`sha256:2405f5fd…` recorded at `/root/rollback_image_id.txt`. Full redeploy facts in
"Droplet state (2026-06-07)" below.

**Shipped this session:**
- `/debug/trigger-fill` (`4457c97`) and `/debug/trigger-settlement` (`00dc0e5`) —
  sim-only verification hatches (404 in live, bearer-authed) that drive the real
  fill / settlement accounting on demand.
- Settlement-P&L round-trip fix (`e11f210`) — `resolution_polling_loop` now persists
  the `realised_pnl` delta, not the equity delta, so losing settlements count as
  losses; **winRate is no longer structurally inflated**.
- Strategy-merge donut fix (`7255010`, the one scoped fence-lift, now re-locked) —
  `on_fill`'s BUY merge attributes the merged position's `strategy` by dominant
  quantity (tie → keep existing) instead of silently keeping the first opener's tag.

**"Make profitability measurable" epic:** DONE + verified live — winRate moved
0 → 100 → 50 on the droplet settlement smoke.

**Open / deferred:**
- (a) Per-strategy attribution **option D** — the dominant-by-quantity donut fix
  (`7255010`) is interim/single-label. Revisit (split positions per strategy) only if
  two strategies co-occupying one `market_id+outcome` key proves common in practice.
- (b) Donut strategy-id — the dashboard color/name map matches the bot's contractual
  `strategy1`/`strategy2`/`strategy3` wire vocabulary: **no drift at the contract
  level, no dashboard change needed** (`intra_arbitrage` / `momentum_latency` are
  display names for those ids, not wire values). **OPEN (bot-side):** a droplet probe
  of the live `openPositions[].strategy` field to confirm `7255010` still emits
  contract-conforming `strategy1/2/3` (cosmetic-only if it drifted — a generic-color
  slice + raw-id label). *Optional dashboard hardening (separate):*
  `OpenPositionSchema.strategy` is a free `z.string()`, not the enum — tightening it
  toward `StrategyIdSchema` (or a `.refine` that logs unknown ids) would surface
  non-conforming tags instead of silently rendering the fallback.
- (c) Dependabot clean-PR merge — pending regeneration of the PR.
- (d) Droplet `*** System restart required ***` — deferred to a maintenance window.
- (e) Residual sim equity on the droplet — left as-is.

## What changed since v0.2.0

> For releases after v0.2.1, see `CHANGELOG.md`. The list below is the original v0.2.1→v0.2.0 delta, preserved for history.

1. **Dependency conflict** — `py-clob-client==0.22.0` triple-pins `eth-account===0.13.0`. The old `web3<7` constraint required `eth-account<0.13`. **Fix:** `web3==7.6.0` plus explicit `hexbytes>=1.2.0,<2.0.0`. Do **not** downgrade `eth-account`.
2. **Multi-stage Dockerfile** — Builder installs wheels with `gcc` / `build-essential`; runtime image is slim Bookworm + copied venv (~280 MB class target; measure on your registry).
3. **`docker-compose` `version:` key removed** — Compose v2 no longer needs it.
4. **`.dockerignore`** — Shrinks build context (order-of-magnitude smaller tarball vs. sending `.git`, caches, `data/`, `logs/`).
5. **`deploy/rebuild.sh`** — Preflight, `compose build --pull`, `up -d`, polls logs for `strategy_loop_started` (60 s), then tails logs unless `--no-tail`.
6. **`scripts/__init__.py`** — Restored as a proper package so `from scripts.weekly_report import generate_report` works from cron / one-shots.

## Verification status

| Check | Where it was validated |
| --- | --- |
| `requirements.txt` reasoning | Upstream `py-clob-client` / `web3` / `eth-account` metadata on GitHub & PyPI (maintainer sandbox) |
| `weekly_report` import + pytest fixtures | Local `pytest tests/test_weekly_report.py` |
| `scripts.wallet_reconcile` import | `python -c "import scripts.wallet_reconcile"` — passes on **web3 7.x** (no `geth_poa_middleware` / POA shim in this tree; `wallet_reconcile_cli` uses plain `Web3.HTTPProvider`). |
| `rebuild.sh` | `bash -n` (shell syntax) |
| **Docker image build** | **Deploy host** — Cursor sandboxes often lack Docker / full network; treat first `./deploy/rebuild.sh` on the droplet as acceptance |
| **`strategy_loop_started` in runtime logs** | **Deploy host** — confirms feeds + loop; depends on Polymarket API reachability |

## Known issues (carried forward)

| Issue | Notes |
| --- | --- |
| Polymarket WS **404** | REST polling fallback; not fixed in v0.2.1 |
| Binance **451** | Geo / compliance; use non-US POP or disable feed; not fixed in v0.2.1 |
| Image size | Slim runtime + venv; still monitor disk on 25 GB droplets |
| ~~Snapshot `positions[].strategy` mis-attribution~~ — **RESOLVED `7255010`** | `on_fill`'s BUY merge now sets `strategy` by dominant quantity (tie → keep existing) in the merge branch of `risk_manager.py`; the corrected tag reaches the wire via `api/routes_rest.py:_build_open_position` (`:123`). Interim single-label fix — see Current-state Open/deferred (a) for the option-D split. |

## Triage runbook

### `pip` / `docker compose build` fails on resolver errors

- Re-read the header comment in `requirements.txt`. Never satisfy `py-clob-client` by downgrading `eth-account`.
- Clear build cache: `./deploy/rebuild.sh --nuke`

### Container exits immediately

- `docker compose logs --tail=200 bot`
- Check `.env` for live-mode required `BOT_POLYMARKET_*` keys when `BOT_MODE=live`.

### No `strategy_loop_started` within 60 s

- Confirm Polymarket discovery returned markets (`no_markets_discovered` warning).
- Confirm outbound HTTPS from the host (firewall / proxy).
- Increase log window manually: `docker compose logs bot | grep strategy_loop`

### Weekly report empty or failing

- `BOT_DATA_DIR/trades.jsonl` is written automatically on every accepted fill by `utils/trades.persist_fill` (d079f93). Empty file = no fills yet in the window; missing file = `BOT_DATA_DIR` mount issue (check the `market_cache_data` volume in `docker-compose.yml` and the explicit `BOT_DATA_DIR=/app/data` env pin).
- Run: `docker compose exec -T bot python -m scripts.weekly_report --weeks 1 -v`

## Open questions for the next maintainer

1. **WebSocket migration** — When Polymarket ships a stable WS URL, re-enable and drop REST-only hot paths where safe.
2. **Reference price diversification** — Reduce reliance on Binance when `DISABLE_BINANCE_FEED=1` (midpoint-only / multi-venue).
3. **Hash-pinning** — DONE. `requirements.txt` is generated from `requirements.in` by `pip-compile --allow-unsafe --generate-hashes` (the resulting lockfile is hash-pinned, and `pip install -r requirements.txt` auto-enforces hash-checking). Edit pins in `requirements.in` and regenerate; never edit `requirements.txt` by hand.

## Open issues for v0.2.3 (observed in v0.2.2 running container)

These three are config/ownership issues, not code bugs. They don't block
v0.2.2 acceptance but should be fixed before the bot moves out of sim mode.

1. **`discord_alert_failed`** — `DISCORD_WEBHOOK_URL` in `.env` is set to
   the literal placeholder `# Discord channel webhook URL`. Either fill it
   in or remove the line.

2. **`equity_reconciliation_disabled: Non-hexadecimal digit found`** — the
   funder address env var has stray characters (likely whitespace, quotes,
   or the placeholder). Reconciliation is disabled until this is fixed,
   so PnL won't be cross-checked against on-chain balances.

3. **`market_cache_write_failed: [Errno 13] Permission denied`** — bot runs
   as UID 1000 (`bot`) but `/opt/polymarket_hybrid_bot` was cloned as root
   on the droplet. Fix is one of:
   - `chown -R 1000:1000 /opt/polymarket_hybrid_bot` (quick fix), or
   - Move the cache path to `/app/data/.market_cache.json` (clean fix —
     `data/` is already a writable bind mount), or
   - Stop running the deploy as root and clone as a non-root `bot` user
     per `DEPLOY_DO.md` (correct fix).

### v0.2.2 verification status update

The two `⚠️` rows in the verification table above are now resolved:
- **Docker build**: ✅ verified on production droplet (Ubuntu 24.04, NYC1)
  on 2026-05-02 after the eth-stack floor bumps shipped in v0.2.2.
- **`strategy_loop_started` log line**: ✅ observed at 2026-05-02T23:24:41Z
  with 26 markets discovered.

## v0.2.3 droplet verification (2026-05-03)

The chown quick-fix from `DEPLOY_DO.md` has been applied on the production
droplet (`root@157.230.63.174`, `/opt/polymarket_hybrid_bot`):

    chown -R 1000:1000 /opt/polymarket_hybrid_bot
    docker compose up -d   # no-op for compose; bind mount picks up new ownership in the running container

Result, verified at 2026-05-03T05:06Z:
- `market_cache_write_failed` no longer appears in recent log tails (was firing
  every cycle before).
- `equity_reconciliation_disabled` now reports the cleaner reason
  `BOT_POLYGON_RPC_URL unset` (was `wallet_address_error: Non-hexadecimal
  digit found`) — confirming the inline-comment cleanup also stuck.
- `discord_alert_failed` still gone (last seen pre-cleanup).
- Container `polymarket_hybrid_bot:0.2.1` healthy, sim mode, 32 markets.

The remaining warnings (`Binance 451`, `Polymarket WS 404`) are
network/upstream issues tracked for v0.3.0 and do not affect the sim-mode
strategy loop.

The proper fix (deploy as a non-root `bot` user) is still tracked for v0.3.0.
The chown is a quick fix that will need to be redone if the droplet is
re-cloned from scratch.

## Droplet state (2026-05-26)

> **Superseded by "Droplet state (2026-06-07)" below.**

Droplet (157.230.63.174) runs `main` @ `2f8bcac` (container image `polymarket_hybrid_bot:0.2.4`, content rebuilt to image ID `4cff62dd`), healthy. The redeploy from `swarm/team-b-phase-d @ 77b3ccd` (tag `v0.2.8`) was EXECUTED per `DROPLET_REDEPLOY.md` — the prior "PRODUCED but DEFERRED (not executed)" framing is RESOLVED. Redeploy chain (per `.swarm-decisions.md`): `77b3ccd` → `b8095bc` (row `b1-fill-runtime-update`) → `54cf1fc` (image `5f415e1a`; row `2b-polling-runtime-closed`) → `2f8bcac` (image `4cff62dd`; per `docs/NEXT_STEPS.md:19`). The 1 GB→2 GB RAM decision noted in the prior framing is no longer gating — the redeploys above were executed on the existing droplet sizing.

## Droplet state (2026-06-07)

Supersedes the 2026-05-26 block above. Droplet (`157.230.63.174`,
`/opt/polymarket_hybrid_bot`) runs `main` @ `57d8b18`, container image
`sha256:89676eb4…`, healthy; deploy lane is `main`. Rollback ref `sha256:2405f5fd…`
recorded at `/root/rollback_image_id.txt` (re-run that image to roll back). CI run
`27096324537` (480 green) gated this deploy.

Carried-forward operational notes:
- `*** System restart required ***` on the droplet — deferred to a maintenance
  window (Open/deferred (d)).
- Residual sim equity left as-is (Open/deferred (e)).
- Network/upstream warnings (`Binance 451`, `Polymarket WS 404`) unchanged — REST
  fallback in place.

> Image digests (`89676eb4`, `2405f5fd`) and the CI run id are operator-supplied;
> they describe droplet / registry / CI state and are not verifiable from this repo.

## Agent discipline

For AI coding agents (Claude Code, Cursor, etc.) working on this repo: see `CLAUDE.md` at repo root for project-specific rules, hard invariants, and `/verify` auto-invocation triggers. That file complements the global `~/.claude/CLAUDE.md` and is the load-bearing contract for all agent sessions.
