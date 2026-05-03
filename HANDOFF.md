# HANDOFF — polymarket_hybrid_bot (v0.2.1)

## TL;DR

```bash
git pull && ./deploy/rebuild.sh
```

First-time on a fresh host: `cp .env.example .env`, fill secrets, `chmod 600 .env`, then `./deploy/rebuild.sh`.

## What changed since v0.2.0

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

- `BOT_DATA_DIR/trades.jsonl` must exist for non-empty aggregates (writer may land in a future release).
- Run: `docker compose exec -T bot python -m scripts.weekly_report --weeks 1 -v`

## Open questions for the next maintainer

1. **WebSocket migration** — When Polymarket ships a stable WS URL, re-enable and drop REST-only hot paths where safe.
2. **Reference price diversification** — Reduce reliance on Binance when `DISABLE_BINANCE_FEED=1` (midpoint-only / multi-venue).
3. **Hash-pinning** — Consider `pip hash` / lockfile for fully reproducible CI once the 7.x stack stabilizes.

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
