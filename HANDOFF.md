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
