# DigitalOcean Deployment Guide
## Droplet: ubuntu-s-1vcpu-1gb-nyc1 | IP: 157.230.63.174

> **Your droplet is 1 vCPU / 1 GB RAM / NYC1 / Ubuntu 24.04 LTS.**
> NYC1 is ideal — Polymarket's infrastructure is US-East, minimising latency.
> 1 GB RAM is tight for Docker. Follow the swap and memory steps below carefully.

---

## Step 0 — One-time droplet hardening (run once, as root)

```bash
ssh root@157.230.63.174

# Create a deploy user (don't run the bot as root)
adduser botuser
usermod -aG sudo botuser
usermod -aG docker botuser

# UFW firewall — allow only SSH
ufw allow OpenSSH
ufw enable
ufw status

# Add 1 GB swap (critical for 1 GB RAM droplet)
fallocate -l 1G /swapfile
chmod 600 /swapfile
mkswap /swapfile
swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab

# Tune swappiness (prefer RAM, only swap under pressure)
echo 'vm.swappiness=10' >> /etc/sysctl.conf
sysctl -p

# Verify swap is active
free -h
```

---

## Step 1 — Firewall and metrics (optional)

The bot exposes **Prometheus `/metrics` on port 9091** (`deploy/docker-compose.yml`). By default UFW only allows SSH.

- **Local scrape only (recommended):** Run Prometheus/Grafana on the same droplet or use an SSH tunnel; no UFW change.
- **Remote scrape:** Restrict source IP, e.g. `ufw allow from <monitoring-ip> to any port 9091 proto tcp`.

Do **not** expose 9091 to `0.0.0.0/0` without auth or IP allowlisting.

---

## Step 2 — Environment variables (v0.2.0)

Copy `deploy/.env.example` → `.env` at the **repository root** (compose mounts `../.env`).

**New or critical variables for this milestone:**

| Variable | Notes |
|----------|--------|
| `BOT_EXPERIMENT_ID` | Required for A/B; must match weekly report filter |
| `BOT_METRICS_ENABLED` / `BOT_METRICS_PORT` | Default `true` / `9091` |
| `BOT_RESOLUTION_POLL_SECONDS` | Gamma settlement poll interval |
| `BOT_FILL_POLL_INTERVAL_SEC` / `BOT_FILL_POLL_TIMEOUT_SEC` | Live order reconciliation |
| `BOT_RECONCILIATION_INTERVAL_SECONDS` / `BOT_RECONCILIATION_DRIFT_THRESHOLD_PCT` | On-chain vs bot equity |
| `BOT_POLYGON_RPC_URL` | **Required** for hourly equity reconciliation and `wallet_reconcile.py` |
| `BOT_POLYGON_WALLET_ADDRESS` | Optional; else derived from `BOT_POLYMARKET_PRIVATE_KEY` |

**CLI-only (not in `BotConfig`):** `BOT_METRICS_HOST`, `BOT_LOG_PATH` for `scripts/wallet_reconcile.py`.

Full list: `deploy/.env.example`.

---

## Step 3 — Polygon RPC rate limits

Equity reconciliation runs **once per `BOT_RECONCILIATION_INTERVAL_SECONDS`** (default 1h) plus manual CLI runs. Each check performs two `balanceOf` calls (USDC.e + native USDC). Use a provider tier that allows this baseline; avoid sub-minute polling against free tiers. If you see `equity_reconciliation_degraded`, upgrade RPC or increase reconciliation interval.

---

## Step 4 — Deploy with Docker

On the droplet as `botuser`:

```bash
cd /path/to/polymarket_hybrid_bot/deploy
docker compose build
docker compose up -d
docker logs -f polymarket-hybrid-bot
curl -s http://127.0.0.1:9091/metrics | head -20
```

Switch to live **only** after validation: change `command` to `["python", "main.py", "--live"]` and confirm credentials.

---

## Step 5 — Optional: weekly report cron

Cron parses the crontab **once** at install time — do **not** put bare `$(date ...)` in the line (the window would freeze). Prefer the repo script (Linux `date`):

1. Edit **`scripts/weekly_report_cron.sh`**: set `REPO_ROOT`, `LOG_GLOB`, `EXPERIMENT_ID`, `OUTPUT_APPEND`.
2. `chmod +x scripts/weekly_report_cron.sh`
3. Crontab (example: Mondays 08:00 UTC):

```cron
0 8 * * 1 /path/to/polymarket_hybrid_bot/scripts/weekly_report_cron.sh
```

**Inline alternative** (dates evaluated each run via Bash):

```cron
0 8 * * 1 /bin/bash -lc 'cd /path/to/polymarket_hybrid_bot && START=$(date -u -d "7 days ago" +%F) && END=$(date -u +%F) && BOT_MODE=sim python scripts/weekly_report.py --logs "/var/log/bot/*.log" --start "$START" --end "$END" --experiment-id default --format markdown >>/var/log/weekly_report.md 2>&1'
```

Use a **systemd timer** with `ExecStart` pointing at the same script or `bash -lc` line. Ensure log shipping writes **one JSON object per line** (structlog JSONRenderer).

---

## Step 6 — Related docs

- `docs/HANDOFF.md` — full operations and observability contract
- `docs/VALIDATION_CHECKLIST.md` — 48–72h sim criteria
- `TESTING_CHECKLIST.md` — smoke tests after deploy
