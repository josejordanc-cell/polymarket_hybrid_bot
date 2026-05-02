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
- **Weekly report fixture:** `python scripts/weekly_report.py --logs tests/fixtures/sample_logs.jsonl --start 2026-04-26 --end 2026-05-02 --experiment-id default --format json`.

Full smoke steps: **`TESTING_CHECKLIST.md`**. Long sim acceptance: **`docs/VALIDATION_CHECKLIST.md`**.
