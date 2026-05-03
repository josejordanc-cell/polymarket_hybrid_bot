# DigitalOcean — droplet deploy (v0.2.1)

## Sizing

- **OS:** Ubuntu 22.04 LTS  
- **CPU / RAM:** **1 vCPU / 2 GB RAM minimum** — native extension builds (e.g. `cytoolz` via `web3`) can OOM on **1 GB** during `pip` / Docker build.  
- **Disk:** 25 GB is workable; monitor Docker images and `logs/`.

## Region vs Binance

- For **live Binance** ccxt feeds, prefer **`fra1`**, **`sgp1`**, or **`lon1`** (lower chance of HTTP **451**).  
- **`nyc*` / `sfo*` / `ams*`** are fine if you run with **`DISABLE_BINANCE_FEED=1`** and accept Polymarket-centric pricing.

## Initial server setup

1. Create a non-root user (e.g. `bot`) with `sudo`.  
2. Harden **sshd** (`PermitRootLogin no`, `PasswordAuthentication no`, deploy SSH keys).  
3. Install **Docker Engine** and **Compose v2** from Docker’s official Ubuntu repository (not deprecated `docker-compose` 1.x).  
4. **UFW:** allow only **OpenSSH** (`ufw allow OpenSSH && ufw enable`).

## Application install

```bash
sudo -u bot -i
git clone <repo> polymarket_hybrid_bot && cd polymarket_hybrid_bot
cp .env.example .env
chmod 600 .env
# Fill BOT_POLYMARKET_* for live; leave BOT_MODE=sim for smoke test.
./deploy/rebuild.sh
```

## Weekly report cron

Mondays 09:00 UTC (container clock):

```cron
0 9 * * 1 cd /home/bot/polymarket_hybrid_bot && docker compose exec -T bot python -m scripts.weekly_report --weeks 1 >>/home/bot/weekly_report.cron.log 2>&1
```

Adjust the `cd` path to your clone location.

## Updates

```bash
cd ~/polymarket_hybrid_bot
git pull
./deploy/rebuild.sh --no-tail
```

## Troubleshooting

| Symptom | Action |
| --- | --- |
| Build OOM | Upgrade droplet to 2 GB; rerun `./deploy/rebuild.sh --nuke` |
| `451` from Binance | Move region or `DISABLE_BINANCE_FEED=1` |
| WS 404 spam | Expected; REST fallback — see `README.md` |
| Healthcheck failing | Ensure `/tmp/.bot_alive` is touched (runtime heartbeat task); check `docker inspect` health |
| No trades in weekly report | `data/trades.jsonl` missing or empty — writer may not exist yet |

## Running as root on the droplet (current state)

The deploy host currently runs the bot as `root` and clones `/opt/polymarket_hybrid_bot`
as `root`. This works but produces one warning at startup:

    {"error": "[Errno 13] Permission denied: '.market_cache.json'",
     "event": "market_cache_write_failed", "level": "warning"}

The bot process inside the container runs as UID 1000 (`bot`), so it can't write
the market cache to a root-owned bind mount. Two ways to fix:

### Quick fix — chown the deploy tree

    chown -R 1000:1000 /opt/polymarket_hybrid_bot
    docker compose up -d

The `up -d` (not `restart`) is required to recreate the container so the new
ownership is picked up by the bind mount. After this, `market_cache_write_failed`
should not appear in the logs.

### Proper fix — deploy as a non-root user (deferred to v0.3.0)

Create a `bot` user on the host with UID 1000, clone the repo as that user,
and SSH in as `bot` for all deploy operations. This eliminates the root-owned
working tree entirely and matches the in-container UID.

Steps (rough sketch — full procedure tracked for v0.3.0):

    adduser --uid 1000 --disabled-password --gecos "" bot
    usermod -aG docker bot
    su - bot -c 'git clone https://github.com/<owner>/polymarket_hybrid_bot.git ~/polymarket_hybrid_bot'

Then deploy from `~/polymarket_hybrid_bot` as `bot`, not from `/opt` as `root`.
