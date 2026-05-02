#!/usr/bin/env bash
# Weekly trades report (v0.2.1+) — runs ``python -m scripts.weekly_report`` which reads
# ``$BOT_DATA_DIR/trades.jsonl`` and appends Markdown to OUTPUT_APPEND.
#
# Edit REPO_ROOT, OUTPUT_APPEND, then:
#   chmod +x scripts/weekly_report_cron.sh
#
# Cron example (Mondays 08:00 UTC):
#   0 8 * * 1 /path/to/polymarket_hybrid_bot/scripts/weekly_report_cron.sh
#
# For Docker-first hosts, prefer: ``docker compose exec -T bot python -m scripts.weekly_report …``
# (see DEPLOY_DO.md).
set -euo pipefail

REPO_ROOT="/path/to/polymarket_hybrid_bot"
OUTPUT_APPEND="/var/log/weekly_report.md"

cd "$REPO_ROOT"
export BOT_MODE="${BOT_MODE:-sim}"
export BOT_DATA_DIR="${BOT_DATA_DIR:-$REPO_ROOT/data}"
export BOT_REPORT_DIR="${BOT_REPORT_DIR:-$REPO_ROOT/data/reports}"

python3 -m scripts.weekly_report --weeks 1 --format md --stdout >>"$OUTPUT_APPEND" 2>&1
