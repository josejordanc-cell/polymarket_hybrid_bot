# polymarket_hybrid_bot

**polymarket_hybrid_bot** is an autonomous Python trading service for **Polymarket** prediction markets. It runs a strict priority loop across three core strategies—intra-market arbitrage (YES+NO edge), spot-lag momentum, and market making—with optional smart-money copy. It operates in **simulation** (paper) or **live** mode against the Polymarket CLOB, with structured logging and Prometheus metrics for operations and dashboards.

## For developers joining the project

Start with **[`docs/HANDOFF.md`](docs/HANDOFF.md)** — architecture, observability contract (logs + `/metrics`), configuration, runbook, and integration notes. **[`docs/FINAL_CODE_REVIEW.md`](docs/FINAL_CODE_REVIEW.md)** and **[`docs/NEXT_STEPS.md`](docs/NEXT_STEPS.md)** cover review findings and priorities.

## Running tests

See **[`TESTING.md`](TESTING.md)** for how to run pytest. **[`TESTING_CHECKLIST.md`](TESTING_CHECKLIST.md)** has quick smoke checks (`/metrics`, CLI tools).

## Deploying

See **[`deploy/DEPLOY_DO.md`](deploy/DEPLOY_DO.md)** (DigitalOcean, Docker Compose, env vars, port **9091** for metrics).

## What state is this in

The project is at **v0.2.0** (pre-dashboard milestone): **sim** mode is the production-stable default for validation; **live** trading is gated on completing **[`docs/VALIDATION_CHECKLIST.md`](docs/VALIDATION_CHECKLIST.md)** and risk sign-off.
