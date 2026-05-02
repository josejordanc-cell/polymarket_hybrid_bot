# Testing checklist — “Is this bot working?”

Quick verification after install or deploy. For multi-day sim acceptance, use **`docs/VALIDATION_CHECKLIST.md`**.

## 1. Unit and integration tests

From repository root, Python **3.11** recommended (matches `deploy/Dockerfile`):

```bash
pip install -r requirements.txt
pytest -q
```

**Pass:** All tests green; no collection errors.

## 2. Smoke: `/metrics`

With the bot running (or a short local run with metrics enabled):

```bash
curl -s "http://127.0.0.1:9091/metrics" | grep -E 'bot_equity_usd|bot_fills_total|experiment_id'
```

**Pass:** Response is Prometheus text format; `bot_equity_usd` and labels include `experiment_id` and `mode`. If `BOT_METRICS_ENABLED=false`, expect connection failure or empty — that is intentional.

## 3. Smoke: `wallet_reconcile.py`

Requires `BOT_POLYGON_RPC_URL` and wallet derivation in `.env`:

```bash
python scripts/wallet_reconcile.py --json
echo $?
```

**Pass:** Exit **0** (within threshold) or **1** (breach) with valid JSON body and numeric `drift_pct`. Exit **2** means RPC/config/metrics+log failure — fix env before production.

Optional:

```bash
python scripts/wallet_reconcile.py --no-metrics --log-file bot.log --threshold 5
```

## 4. Smoke: `weekly_report.py` with fixture

```bash
python scripts/weekly_report.py \
  --logs "tests/fixtures/sample_logs.jsonl" \
  --start 2026-04-26 \
  --end 2026-05-02 \
  --experiment-id default \
  --format json | python -m json.tool | head -40
```

**Pass:** Exit 0; JSON contains `experiment_id`, `strategies`, `total_pnl_usd`, `max_intraweek_drawdown_pct`.

Strictness check (should exit 2 and list experiments on stderr):

```bash
python scripts/weekly_report.py --logs "tests/fixtures/sample_logs.jsonl" --start 2026-04-26 --end 2026-05-02 2>/dev/null; test $? -eq 2
```

## 5. Docker (optional)

```bash
cd deploy && docker compose build && docker compose up -d
docker exec polymarket-hybrid-bot curl -s http://127.0.0.1:9091/metrics | head -5
```

**Pass:** Container healthy; `/metrics` returns data.

---

See also **`TESTING.md`** for minimal pytest invocation.
