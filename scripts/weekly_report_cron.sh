#!/usr/bin/env bash
# Weekly report cron helper (Ubuntu / Linux). Edit REPO_ROOT, LOG_GLOB, EXPERIMENT_ID, OUTPUT_APPEND, then:
#   chmod +x scripts/weekly_report_cron.sh
# Cron example (Mondays 08:00 UTC):
#   0 8 * * 1 /path/to/polymarket_hybrid_bot/scripts/weekly_report_cron.sh
set -euo pipefail

REPO_ROOT="/path/to/polymarket_hybrid_bot"
LOG_GLOB="/var/log/bot/*.log"
EXPERIMENT_ID="default"
OUTPUT_APPEND="/var/log/weekly_report.md"

cd "$REPO_ROOT"
START=$(date -u -d "7 days ago" +%F)
END=$(date -u +%F)

BOT_MODE=sim python scripts/weekly_report.py \
  --logs "$LOG_GLOB" \
  --start "$START" \
  --end "$END" \
  --experiment-id "$EXPERIMENT_ID" \
  --format markdown >>"$OUTPUT_APPEND" 2>&1
