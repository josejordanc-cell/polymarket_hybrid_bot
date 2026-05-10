# polymarket_hybrid_bot

Async Polymarket hybrid strategies (sim default): feeds → risk → priority strategy loop → CLOB execution.

## Status (post-v0.2.4)

| Area | Status |
| --- | --- |
| Docker build | Fixed — `web3` 7.x + explicit `hexbytes` pin resolves `eth-account===0.13.0` conflict with `py-clob-client==0.22.0` |
| Strategy loop | OK — look for `strategy_loop_started` in logs after boot |
| REST (Polymarket / Gamma) | Primary data path |
| WebSocket (Polymarket CLOB) | **Degraded** — CLOB WebSocket upgrade may return **404**; the bot uses REST polling as the stable operational fallback |
| Binance spot feed | **Disabled by default in restricted regions via** `BOT_DISABLE_BINANCE_FEED=true` (added in T11). Without the flag, US/most DO POPs still receive HTTP 451 from `api.binance.com`. Coinbase remains the live reference feed in those regions. |
| Weekly report (`scripts/weekly_report.py`) | Reads `data/trades.jsonl` JSONL; empty / missing file handled gracefully |

## Quick start

```bash
git clone <your-fork-url> polymarket_hybrid_bot
cd polymarket_hybrid_bot
cp .env.example .env
# Edit .env — at minimum leave BOT_MODE=sim for first boot.
./deploy/rebuild.sh
```

## Working build commands

Exact sequence (same as the wrapper, minus log polling):

```bash
docker compose down --remove-orphans
docker compose build --pull
docker compose up -d
docker compose logs -f --tail=50 bot
```

One-command rebuild (blocks until `strategy_loop_started` or times out):

```bash
./deploy/rebuild.sh              # follow logs on success
./deploy/rebuild.sh --no-tail    # exit after readiness check
./deploy/rebuild.sh --nuke       # prune image/build cache before build
```

## Project layout

```
polymarket_hybrid_bot/
├── main.py                 # Async runtime entry (sim/live)
├── polymarket_hybrid_bot/  # Package shim: python -m polymarket_hybrid_bot.main
├── config/                 # Pydantic settings (BOT_* env)
├── data_feeds/             # Polymarket, Binance/Coinbase, Polygon
├── strategies/             # Arb, momentum, MM, smart-money
├── execution/              # CLOB executor + sim
├── deploy/
│   └── rebuild.sh          # Idempotent compose rebuild + readiness poll
├── scripts/
│   ├── weekly_report.py    # trades.jsonl weekly aggregate
│   └── wallet_reconcile.py
├── data/                   # Mounted volume — trades.jsonl, reports
├── logs/                   # Mounted volume
├── Dockerfile              # Multi-stage builder → slim runtime
├── docker-compose.yml      # Service `bot`, image polymarket_hybrid_bot:0.2.4
├── requirements.txt        # Pinned deps (v0.2.1)
├── HANDOFF.md              # Maintainer handoff
└── DEPLOY_DO.md            # DigitalOcean droplet guide
```

## Configuration (`.env.example`)

| Variable | Purpose |
| --- | --- |
| `POLY_PK` | Doc alias — set `BOT_POLYMARKET_PRIVATE_KEY` to the same value for live |
| `POLY_FUNDER` | Funder / proxy wallet (Polymarket L2 flows) |
| `POLY_SIGNATURE_TYPE` | Signature type hint (see Polymarket docs) |
| `POLY_HOST` | CLOB HTTP base (alias for `BOT_POLYMARKET_HOST`) |
| `POLY_CHAIN_ID` | Chain ID (alias for `BOT_CHAIN_ID`) |
| `POLY_RPC_URL` | Polygon JSON-RPC (alias for `BOT_POLYGON_RPC_URL`) |
| `BOT_MODE` | `sim` (default) or `live` |
| `BOT_LOG_LEVEL` | `DEBUG` … `ERROR` |
| `BOT_DATA_DIR` | Data directory (default `/app/data` in Docker) |
| `BOT_LOG_DIR` | Log directory (`/app/logs` in Docker) |
| `BOT_REPORT_DIR` | Weekly report output (`/app/data/reports` in Docker) |
| `BOT_DISABLE_BINANCE_FEED` | Skip the Binance reference feed (set `1` / `true` / `yes`). Use in regions where `api.binance.com` returns HTTP 451. Coinbase feed unaffected. |
| `BOT_MARKET_CACHE_PATH` | Filesystem path inside the container for the markets discovery cache (default `/app/data/market_cache.json`). Backed by the `market_cache_data` Docker named volume so the cache survives rebuilds. Override only if running outside Docker or with a custom mount. (T10) |

See `.env.example` for the full `BOT_*` surface (metrics, resolution poll, API keys).

## Known issues

1. **Polymarket WebSocket 404** — The WS upgrade URL may return 404; the bot continues on REST. Not introduced in v0.2.1.
2. **Binance 451** — `api.binance.com` returns HTTP 451 from US and most DigitalOcean POPs. Set `BOT_DISABLE_BINANCE_FEED=true` in `.env` to silence the noise (T11), or run from `fra1` / `sgp1` / `lon1` if you need the live Binance reference feed.

## Operations cheatsheet

| Task | Command |
| --- | --- |
| Follow logs | `docker compose logs -f bot` |
| Weekly report (host, repo on disk) | `docker compose exec -T bot python -m scripts.weekly_report --weeks 1 --stdout` |
| Restart stack | `./deploy/rebuild.sh --no-tail` |
| Stop | `docker compose down` |
| Update code | `git pull && ./deploy/rebuild.sh` |

## More documentation

- `HANDOFF.md` — TL;DR + triage for maintainers  
- `DEPLOY_DO.md` — DigitalOcean sizing, regions, UFW, cron  
- `CHANGELOG.md` — Release notes  
- `docs/` — Deeper design notes from pre–v0.2.1 milestones  

## Repository Files

Top-level files in this repo:

- `CLAUDE.md` — project-level agent discipline rules and observability invariants for AI coding agents working on this repo. Complements `~/.claude/CLAUDE.md` (global). See file for the 12 hard rules and `/verify` auto-invocation triggers.
- `LICENSE` — proprietary license. All Rights Reserved.
- `HANDOFF.md` — deployment history and incident runbook.
- `SECURITY.md` — disclosure policy.
- `CHANGELOG.md` — version history.
- `TESTING_CHECKLIST.md` — quick smoke tests.
- `README.md` — this file.

Documentation under `docs/`:

- `docs/HANDOFF.md` — architecture, strategy priority, and observability contract (load-bearing for any metrics/strategy work).
- `docs/VALIDATION_CHECKLIST.md` — pre-live-flip gate.
- `docs/FINAL_CODE_REVIEW.md` — engineering review.
- `docs/NEXT_STEPS.md` — current priority list.
- `docs/SECURITY_REVIEW.md` — v2 documentation security review.

Deploy under `deploy/`:

- `deploy/DEPLOY_DO.md` — droplet, UFW, and cron runbook.
