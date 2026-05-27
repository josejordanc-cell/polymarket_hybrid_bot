## Running tests

Use **Python 3.11** (see `deploy/Dockerfile`). Python 3.9 is not supported (`dataclass(slots=True)` in `risk_manager`).

```bash
pip install -r requirements.txt
pytest
pytest tests/test_weekly_report.py tests/test_metrics_exporter.py -q   # focused smoke
```

## Additional scenarios (post–v0.2.0)

- **Metrics:** Start the bot with `BOT_METRICS_ENABLED=true`, `curl http://127.0.0.1:9091/metrics`.
- **Wallet reconcile:** `python scripts/wallet_reconcile.py --json` (needs `BOT_POLYGON_RPC_URL`).
- **Weekly report fixture:** seed `$BOT_DATA_DIR/trades.jsonl` from `tests/fixtures/sample_trades.jsonl` then run `python -m scripts.weekly_report --weeks 1 --format json --stdout`. The current CLI accepts only `--weeks N`, `--format {md,json}`, `--stdout`, `-v` (`scripts/weekly_report.py:266-282`); the v0.2.0-era `--logs`/`--start`/`--end`/`--experiment-id` flags were retired with the JSONL rewrite in `554e2a8`. See `TESTING_CHECKLIST.md` §4 for the full `BOT_DATA_DIR`/`BOT_REPORT_DIR` snippet.

Full smoke steps: **`TESTING_CHECKLIST.md`**. Long sim acceptance: **`docs/VALIDATION_CHECKLIST.md`**.
