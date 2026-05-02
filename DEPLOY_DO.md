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
