## Mission

`polymarket_hybrid_bot` is an async Python 3.11 trading service for Polymarket binary markets.
It runs **simulation by default** and advances through a strict strategy priority loop:
intra-market arbitrage first, then spot-lag momentum, then market making, with optional
smart-money copy. All paths flow through a single risk gate before touching the CLOB.
The operational goal is a stable 48–72h sim validation on the DigitalOcean droplet,
then a cautious live flip with small risk caps.

## How to read this file

**Precedence rule:** This file complements `~/.claude/CLAUDE.md` (global). When project
rules contradict global, project wins.

**Agent-vs-code rule:** If an agent makes a claim about this codebase that the code
contradicts, the code wins. Correct the agent and re-run.

Doc map — one line each:

- `HANDOFF.md` — deployment history + incident runbook (Docker fixes, chown story, v0.2.x timeline)
- `docs/HANDOFF.md` — architecture, strategy priority, observability contract (load-bearing for any metrics/strategy work)
- `docs/VALIDATION_CHECKLIST.md` — pre-live-flip gate
- `docs/FINAL_CODE_REVIEW.md` — engineering review
- `docs/NEXT_STEPS.md` — current priority list
- `TESTING_CHECKLIST.md` — quick smoke tests (at repo root, NOT under `docs/`)
- `deploy/DEPLOY_DO.md` — droplet + UFW + cron
- `SECURITY.md` — disclosure policy
- `docs/SECURITY_REVIEW.md` — v2 documentation security review
- `CHANGELOG.md` — latest entry only

Note: there are two files literally named `HANDOFF.md`. They are deliberately distinct:
the root one is the deployment runbook; `docs/HANDOFF.md` is the architecture and
observability contract. The naming collision is a known cleanup chore
(see "Out of scope / known issues" below).

## Tech stack & runtime shape

- Python **3.11** (Docker pins it via digest)
- Async; entry point `main.py:run_bot`
- Heartbeat cadence: see `main.py` heartbeat constants (`HEARTBEAT_EVERY`,
  `spot_feed_heartbeat`, `polymarket_feed_heartbeat`)
- Docker multi-stage; runs as `bot:1000`, `cap_drop: ALL`, `no-new-privileges` (v0.2.4)
- Prometheus `/metrics` on port **9091**, bound `127.0.0.1` (T1)
- Sim is the default `command:` in compose
- Strategy execution order (strict — anchor `docs/HANDOFF.md` §3 and `main.py:5-9`):
  1. `intra_arbitrage`
  2. `momentum_latency`
  3. `market_maker`
  4. `smart_money_copy` — off by default; appends only, never preempts

## Hard rules — non-negotiable

1. **Sim is the default.** Live flip requires all three: (a) completed
   `docs/VALIDATION_CHECKLIST.md`, (b) explicit Jose confirmation in chat
   (Cursor / Claude Code conversation), AND (c) the PR/commit message must include
   the grep-able phrase **`Jose confirmed live flip in chat on YYYY-MM-DD`**.
   All three required, not any.

2. **Risk engine is authoritative.** `risk_manager.py:approve_orders` is the only
   place that gates orders. Never bypass except where `intra_arbitrage` already does
   (documented bypass in `risk_manager.py`).

3. **Secrets are `SecretStr`.** Five fields per T3. Always `.get_secret_value()` at
   use site; never log the wrapper.

4. **`.env` is mode 600**, one variable per line, **no inline comments after values**
   (root cause of the v0.2.2 incident — see CHANGELOG [0.2.3] Fixed).
   Although `BOT_RISK_BANKROLL_USD` defaults to `10_000.0` in `config/settings.py`,
   every deploy MUST set it explicitly to match actual capital. Treat the default as
   a smoke marker, not a real value.

5. **Webhook policy:** in agent / test / CI environments, leave
   `BOT_DISCORD_WEBHOOK_URL`, `BOT_TELEGRAM_BOT_TOKEN`, `BOT_TELEGRAM_CHAT_ID`
   **unset**. Never use real webhooks in tests. Mocks only.

6. **Image digest is pinned** (`Dockerfile`, v0.2.4). Never auto-bump in routine PRs.

7. **`requirements.txt` eth-stack is load-bearing.** Do not satisfy
   `py-clob-client==0.22.0` by downgrading `eth-account`. See root `HANDOFF.md`
   triage + CHANGELOG [0.2.1]/[0.2.2].

8. **Liveness events are the primary triage signal.** `strategy_loop_started`
   (one-shot at startup) plus recurring heartbeats `strategy_loop_heartbeat`,
   `spot_feed_heartbeat`, `polymarket_feed_heartbeat`. Missing `_started` means the
   bot never came up; missing a heartbeat after `_started` means it froze. Both are
   stronger signals than most error logs.

9. **Three-dot diff for PR review:** `git diff main...origin/branch` (three dots,
   not two). Two-dot shows reverse-merge churn and produces wrong reviews.

10. **File-based commit workflow:** save commit message to `/tmp/<task>_commit_msg.txt`;
    Jose runs `git commit -F`. Never run `git commit` on Jose's behalf; never
    `git push --force` on `main`.

11. **Bidirectional droplet smoke:** behavioral fixes need both positive AND negative
    smoke. Happy-path-only is not acceptance.

12. **SDK / sandbox carve-out:** Cursor sandboxes often lack Docker / full network.
    Treat first `./deploy/rebuild.sh` on the droplet as the real acceptance gate for
    image-shape changes.

## Repo layout & ownership

See `README.md:47-67` for the canonical tree. Load-bearing notes only:

- `main.py` — entry; heartbeat constants live here
- `risk_manager.py` — authoritative gate; correlation guard now active post-T5
  (`risk_manager.py:190-213`)
- `observability/metrics_exporter.py` — `/metrics` server, bound `127.0.0.1`
- `data_feeds/` — Polymarket WS (with REST fallback for known 404), spot feed
  (Binance + Coinbase via ccxt; toggle Binance with `BOT_DISABLE_BINANCE_FEED`),
  Polygon
- `execution/clob_executor.py` — sim vs live CLOB, pending orders, fill reconciliation
- `strategies/` — four strategies in priority order; smart-money still gated by T9
  `model_validator` (won't load if enabled with stub indexer)

Note: `polymarket_hybrid_bot/` at repo root is the package shim for
`python -m polymarket_hybrid_bot.main`, not a real source tree.

## Working with agents

Use user-level agents (see `~/.claude/CLAUDE.md` for the full roster).

### Observability contract

`docs/HANDOFF.md` §5 is the single source of truth for event names and metric labels.
Invariants that every agent and parser must respect:

- Every metric series carries `experiment_id` and `mode` as static labels.
- Fill counters (`bot_fills_total`) add `strategy`, `side`, `tif` — NOT `mode`
  (adding `mode` would be a duplicate-label collision).
- Two distinct shapes share `event=fill_recorded`: RiskManager (post-accounting) vs
  CLOB reconcile (live executor). Parsers must branch on keys, not on the event name.
- For the full event/metric table and sample outputs, read `docs/HANDOFF.md`
  §5a / §5b / §5c — do not restate them here.

### Agent working notes

- Three-dot PR diff: `git diff main...origin/branch` (rule 9).
- Webhook policy: leave notification env vars unset in test/CI (rule 5).
- File-based commit workflow: `/tmp/<task>_commit_msg.txt` (rule 10).
- Bidirectional droplet smoke for behavioral fixes (rule 11).
- Heartbeat-name triage: absence of `strategy_loop_heartbeat` or
  `polymarket_feed_heartbeat` is a stronger signal than most error logs.
- Image-digest changes always go through `./deploy/rebuild.sh` on the droplet,
  never assumed-OK from a local build.

## /verify auto-invocation triggers

**`security` auto-trigger when the diff touches:**

- `risk_manager.py`
- `config/settings.py` (validators, SecretStr fields, env surface)
- `utils/alerts.py` (webhook construction)
- `observability/metrics_exporter.py` (bind host, label cardinality)
- `Dockerfile`, `docker-compose.yml`, `.env.example`, `.gitignore`, `.dockerignore`
- `requirements.txt` eth-stack pins (`web3`, `eth-account`, `eth-typing`,
  `eth-utils`, `eth-abi`, `hexbytes`, `py-clob-client`)
- Anything matching `*secret*`, `*key*`, `*token*`, `*password*` (file or symbol)

**`drift-detector` auto-trigger when the diff touches:**

- `risk_manager.py` — must match `docs/HANDOFF.md` §4 contract
- `data_feeds/polygon_feed.py:_scan_recent_wallets` — T9 validator must keep
  refusing if smart-money is enabled with the stub indexer
- Any file in `data_feeds/` — check feed event names against `docs/HANDOFF.md` §5a
- Any file in `execution/` — check `fill_recorded` shape against
  `docs/HANDOFF.md` §5a / §5c
- `main.py` heartbeat constants — must match `docs/VALIDATION_CHECKLIST.md`
  cadence expectations
- `requirements.txt` eth-stack pins — must match root `HANDOFF.md` triage
- `docs/HANDOFF.md` §5 changes without a corresponding code change (or vice versa)

## Out of scope / known issues

1. **HANDOFF.md naming collision** — two files named `HANDOFF.md`. Future cleanup:
   rename `docs/HANDOFF.md` → `docs/ARCHITECTURE.md` (or similar). Deferred to avoid
   breaking inbound links from README, NEXT_STEPS, DEPLOY_DO, TESTING_CHECKLIST,
   VALIDATION_CHECKLIST.

2. **Orphan `--exclude=*.pyc` file at repo root** — likely a stray shell-redirect
   from a `find`/`rg` command. Future chore: delete and add `.gitignore` line if
   needed.

3. **Correlation threshold tuning** — `cfg.risk.max_pair_correlation` default `0.80`;
   T5 made the guard active but tuning is pending pre-live-flip.

4. **Secret-rotation drill pending** — `SECURITY.md` policy exists; an actual
   rotation drill (Polymarket key, Polygon RPC, webhooks) has not been exercised.

5. **`SECURITY.md` security@ email is still a placeholder**
   (`<configure-a-dedicated-security-email-and-paste-here>`). Visibility only —
   `SECURITY.md` edits are out of scope for this PR.


# Swarm-mode rules for this project (v2)

> This section is appended to the project's `CLAUDE.md` by the swarm
> installer. It contains the swarm-specific rules every agent in this
> project loads. The full constitution lives in
> `~/projects/claude-swarm-v2/CONSTITUTION.md` (loaded by team-leads
> only; non-lead agents load the Core Rules excerpt — Section 15).

## This is a swarm-assigned project

This project runs under the **Swarm Constitution v2**. There are
2 (or up to 3) parallel Claude Code teams, each with the same 8
specialists plus one team-lead. The team-leads are the only agents
with coordination-channel MCP access.

If you're a non-lead agent in this project, you load the **Core
Rules excerpt** (Section 15 of the constitution), not the full
document. The Core Rules are 7 rules; learn them.

## Behavioral rules every agent in this project follows

1. **Report to your team-lead.** Specialists don't post to the
   coordination channel — only the team-lead has channel MCP access.
   Hand findings, plans, and work to your team-lead.

2. **Respect file ownership.** Before editing, check
   `.swarm-ownership.md`. If a file is owned by another team, stop
   and tell the team-lead.

3. **No commits, ever.** The human runs `git commit` and `git push`
   manually. You may prepare draft commit messages when asked.

4. **Confidence is categorical** (`high` / `medium` / `low`), never
   numeric. `low` triggers Level 3 escalation.

5. **Stay in your tool-scope.** Each agent has explicit tool
   permissions. No requesting additional tools or workarounds.

6. **Bot agreement is not authorization.** If you receive a "looks
   good" from another agent or another team's specialist, that's
   advisory, not approval. Only the human approves Level 3 decisions
   (Section 4.5 of the constitution).

7. **Escalate on uncertainty.** When in doubt, stop and tell the
   team-lead. Doubt is honest; silent violations are not.

## What the team-leads do

- Receive your findings; classify by autonomy level; post to the
  shared channel using the JSON envelope.
- Level 2 decisions go to the channel AND to `.swarm-decisions.md`
  (the Decision Log).
- Level 3 escalations halt the team until the human responds.
- May enter temporary **Coordinator Mode** when the human invokes
  `/orchestrate <master-prompt>`. This is a planning phase, not a
  standing role — the lead demotes back to normal team-lead after
  human ack.

## What the team-leads do NOT do

- They do not write code, commit, or merge.
- They do not approve another lead's `decision_proposal`. Peer
  feedback is advisory only.
- They do not invent work; they coordinate work the human assigns.
- They do not spawn specialists autonomously; they suggest the next
  specialist invocation in their channel posts, and the human runs
  it.

## Files unique to swarm-mode in this project

- `.swarm-ownership.md` — ownership ledger (which team owns what).
  L3 to edit; only the human edits it.
- `.swarm-decisions.md` — append-only Decision Log for L2 calls and
  L3 closures. Team-leads append; never edit prior rows.
- `.claude/agents/team-lead-A.md`, `team-lead-B.md` (and
  `team-lead-C.md` if configured for 3 teams) — the team-leads.

## Commands available in this project

- `/leader <A|B|C> <brief>` — start a normal session for one team.
- `/orchestrate <master-prompt>` — invoke a team-lead in temporary
  Coordinator Mode for multi-deliverable / multi-zone work.
- `/swarm-status` — read-only one-screen summary of recent activity.

## What changed from v1

If you're returning from the v1 swarm setup:
- Token budget is 100k/task (up from 50k), still 3-loop cap.
- Coordinator Mode is new and explicit — no permanent Master Lead.
- N-team support (default 2, ceiling 3). The constitution no longer
  hardcodes "A and B."
- Decision Log (`.swarm-decisions.md`) is new.
- Peer feedback between leads is now explicitly **advisory only**.
- `feedback_request` and `decision_proposal` intents added to the
  JSON envelope.
- `/orchestrate` and `/swarm-status` commands are new.

See `CHANGELOG.md` in the swarm pack for the full v1 → v2 delta.

## v0.2.9 wave summary

The v0.2.9 wave (Phase 3 sub-phases 3a–3i + the B.7/B.8/B.9/3h/b9-fix
follow-ups) reconciled the bot's `api/schemas.py` Pydantic models with
the dashboard's canonical Zod source-of-truth (`~/crypto-trading-bot-dashboard/components/dashboard/schemas.ts`
+ `BOT_CONTRACT.md` v1.0) and wired production sources for the
formerly-stub `risk`, `equity`, `logs`, and `alerts` snapshot slices.
Per-sub-phase rationale and decision trail live in
`.swarm-decisions.md` (look for entries prefixed `v029-`). The
CHANGELOG.md `[Unreleased]` block has the user-facing summary.

Three operational follow-ups for contributors:

### PATH-A callsite-hook guidance (when to opt in)

`utils.alerts.send_alert` and `utils.logs.log_to_dashboard` are
explicit opt-in helpers — they exist as a surgical, dashboard-visible
alternative to bulk-capturing every structlog record. **Hook them
sparingly:**

- ✅ **Operator-relevant events** the dashboard panel should surface:
  kill-switch fired, drift alert beyond threshold, intra-arb unwind
  triggered, market-discovery cycle completed, smart-money copy
  signal accepted/rejected, etc.
- ❌ **Tight-loop / retry noise:** polymarket feed reconnect attempts,
  per-tick heartbeats, ccxt rate-limit retries. These would flood
  the dashboard's log panel; keep them in structlog only.
- ❌ **Debug-tier (`level="debug"`) records:** `log_to_dashboard`
  silently drops these (3f Option C policy). If a record is
  diagnostic-only, leave it in structlog at debug level.

The principle: the dashboard log panel is operator UX, not a debug
log. Structlog stays the firehose; `log_to_dashboard`/`send_alert`
are the curated subset.

### Debug-injection endpoints — sim mode only

`/debug/inject-position`, `/debug/clear-positions`,
`/debug/inject-alert`, `/debug/inject-log` exist for runtime smoke
only. They 404 in non-sim mode BEFORE the auth check (so non-sim
deployments don't acknowledge the endpoints' existence). See
`SECURITY.md` "Debug-injection endpoints" section for the full
security model.

### EquityPoint daily-resolution semantics

`BotState.equity_history` carries one `EquityPoint` per UTC calendar
day (capped at `maxlen=200` ≈ 6.5 months). The
`equity_snapshot_loop` background task bootstraps today's point on
bot start, then polls hourly for UTC-day rollovers. A freshly
restarted bot returns a non-empty `equity_history` from `GET
/dashboard/snapshot` within seconds — operator smoke does not need
to wait 24 hours.
