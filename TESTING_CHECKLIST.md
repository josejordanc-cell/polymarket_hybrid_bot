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

The current report is driven by `BOT_DATA_DIR/trades.jsonl` (not structlog files). Point `BOT_DATA_DIR` at a tmp dir seeded with the trades fixture, then invoke the CLI:

```bash
TMPDIR=$(mktemp -d)
cp tests/fixtures/sample_trades.jsonl "$TMPDIR/trades.jsonl"
BOT_DATA_DIR="$TMPDIR" BOT_REPORT_DIR="$TMPDIR/reports" \
  python -m scripts.weekly_report --weeks 8 --format json --stdout \
  | python -m json.tool | head -40
```

**Pass:** Exit 0; JSON contains `week_start`, `week_end`, `trades_total`, `trades_filled`, `realized_pnl_usdc`, `fees_paid_usdc`, `sharpe`, `sortino`, `notes`. With the bundled `sample_trades.jsonl` fixture under `--weeks 8`, expect `trades_total == 2` / `trades_filled == 1` (matches `tests/test_weekly_report.py::test_generate_report_sample_fixture_window`). `sharpe` / `sortino` will be `null` (fixture has fewer than 20 daily returns — honesty gate, see `utils.risk_metrics.compute_sharpe` / `compute_sortino`).

Empty-trade-log check (current CLI does NOT exit 2 on missing input — it returns exit 0 with an explanatory note):

```bash
EMPTY=$(mktemp -d)
BOT_DATA_DIR="$EMPTY" BOT_REPORT_DIR="$EMPTY/out" \
  python -m scripts.weekly_report --weeks 1 --format json --stdout \
  | python -c 'import json,sys; r=json.load(sys.stdin); assert r["trades_total"]==0; assert any("No trade log" in n for n in r["notes"]), r["notes"]'
```

## 5. Docker (optional)

```bash
cd deploy && docker compose build && docker compose up -d
docker exec polymarket-hybrid-bot curl -s http://127.0.0.1:9091/metrics | head -5
```

**Pass:** Container healthy; `/metrics` returns data.

---

See also **`TESTING.md`** for minimal pytest invocation.
