# D.2.4 — Emit-Site Config Audit

**Status:** GATE CLOSED — verified 2026-05-13 against `config/settings.py` at v0.2.7 (`d39696a`). Source changes for D.2.4 may begin.

**Last updated:** 2026-05-13
**Anchored to:** bot v0.2.7 (`d39696a`), tag object `7eecc9e9`
**Companion docs:**
- Dashboard contract: `crypto-trading-bot-dashboard/docs/audit/d3-schema-audit.md` (frozen anchors: `api/schemas.py` blob `8ccdb63c`, `api/routes_rest.py` blob `8ae836cf`)
- Dashboard runbook: `crypto-trading-bot-dashboard/docs/audit/d3-smoke-runbook.md`

---

## 1. Purpose

D.2.4 wires the D.2.3 WebSocket broadcaster (`api/emit.py`) into the bot's strategy loop's **fill**, **alert**, and **strategy-update** emit sites. Each new emit site introduces nested-config attribute reads. This document is the **gate** that opens before each such read is added: every `config.<field>` access in proposed D.2.4 source must appear in the table below, with the matching field name in `config/settings.py` verified against the real `BotConfig` (or nested model) class — not against a test-fixture MagicMock.

## 2. Motivation (the v0.2.7 hotfix)

In v0.2.7 (2026-05-13), `api/auth.py` and `api/server.py` read `getattr(config, "api", None)` to fetch the HTTP API sub-config. The actual attached attribute name on `BotConfig` is `http_api`. **Both attributes exist as real fields on `BotConfig`** — `api: ApiConfig` (Polymarket market-data credentials) and `http_api: HttpApiConfig` (dashboard-facing HTTP/WS) — so the bug was reading the *wrong real field*, not a non-existent one. The unit tests passed because the `bot_config` fixture in `tests/api/` is a bare `MagicMock()` without `spec=`, and bare MagicMocks auto-vivify any attribute path. The bug only surfaced during droplet acceptance smokes where the real `BotConfig` instance returned an `ApiConfig` (with empty Polymarket secrets in sim mode) for `config.api`, which didn't have the expected `bot_api_token` / `ws_ticket_secret` fields, breaking auth and the `/ws` upgrade.

Fix landed in two commits:
- `7c0e313` — 2-line source fix (`config.api` → `config.http_api` in both files)
- `d39696a` — 5-file fixture realignment

**D.2.4 inherits the same exposure surface and then some**, because the strategy-loop emit sites will read many more nested-config fields than auth did. This audit is the gate that prevents a repeat of that pattern.

## 3. Methodology

Each row in §5 below is filled by the following procedure:

1. Inspect `config/settings.py` directly. List every top-level field on `BotConfig` and every nested model's field names. **Do not infer from tests, fixtures, or other source files.** The fixture is the trap, not the truth.
2. For each emit site planned, list every `config.<x>` and `config.<x>.<y>` access the new code will perform.
3. For each access, fill the "Verified field name" column with the exact field name as it appears in the model definition AND the model class that owns it (e.g. `BotConfig.http_api → HttpApiConfig.bot_api_token`).
4. If any access does not resolve to a real field in a real model, **stop** and either correct the planned code or extend `BotConfig` first.
5. **Additionally** (new lesson from v0.2.7): if two real fields could plausibly satisfy the access — as `BotConfig.api` and `BotConfig.http_api` both could for "the HTTP API config" — explicitly note which one is correct and why. Wrong-real-field bugs are not caught by `spec=`.
6. When every row's verification cell is filled, flip the **Status** line at the top of this file to `GATE CLOSED — verified <date> against config/settings.py at <commit>`.

## 4. Test-fixture hardening — what does and does NOT defend

The fixture-trap story from v0.2.7 has two distinct failure modes; they need different defenses.

**Failure mode A: non-existent attribute access** (typo, deleted field). Defense: `MagicMock(spec=BotConfig)`. The `spec=` argument makes attribute access on any non-spec'd name raise `AttributeError`.

**Failure mode B: wrong-real-field access** (the actual v0.2.7 bug). Defense: **NOT** `spec=` alone. Both `api` and `http_api` are real spec'd attributes on `BotConfig`, so `spec=` passes either access. Defenses:

1. **Preferred:** test against a real instantiated `BotConfig` with sim-mode defaults. Code that reads the wrong sub-config gets a real object back that doesn't have the expected leaf attributes, and assertions fail loudly.
2. **Faster:** spec each sub-config separately (`MagicMock(spec=HttpApiConfig)`), so code that does `sub_config.bot_api_token` against the right shape passes and against the wrong shape (an `ApiConfig`) raises.

Track as a follow-up commit, not D.2.4-blocking:

- [ ] Migrate `tests/api/conftest.py` `bot_config` fixture to either real-instance (Option A) or per-sub-config-spec (Option B)
- [ ] Add regression test asserting `isinstance(config.http_api, HttpApiConfig)`, `isinstance(config.api, ApiConfig)`, and any other nested-config-type checks used by D.2.3/D.2.4 source

## 5. Planned emit sites — verified

### 5.0 `BotConfig` field surface relevant to D.2.4

Verified against `config/settings.py` at v0.2.7.

| Path | Type | Notes for D.2.4 |
|---|---|---|
| `config.api` | `ApiConfig` | **Polymarket market-data config.** NOT the dashboard HTTP API. Emit sites should not normally need this. |
| `config.http_api` | `HttpApiConfig` | Dashboard HTTP/WS API. Fields: `enabled`, `bind_host`, `port`, `bot_api_token`, `ws_ticket_secret`. Already consumed by D.2.3 broadcaster init; D.2.4 emit-site code should not access this directly. |
| `config.risk` | `RiskConfig` | Fields: `bankroll_usd`, `max_trade_pct`, `daily_drawdown_limit_pct`, `kelly_fraction`, `max_pair_correlation`, `max_open_positions`, `min_order_usd`. Potentially relevant to ALERT severity classification. |
| `config.strategy` | `StrategyConfig` | **Boot-time strategy enable flags and thresholds.** NOT the runtime per-strategy state the dashboard reads — that lives in `api/state.py` under `state.strategy_configs[<id>]`. See §6.0. |
| `config.runtime` | `RuntimeConfig` | `mode` (`"sim"`/`"live"`), `log_level`, polling intervals. |
| `config.metrics_enabled`, `config.experiment_id`, etc. | various | Root-level fields. Not expected to be touched by D.2.4. |

### 5.1 `FILL` emit

| Field | Value |
|---|---|
| Trigger | Fill reported back from executor. Verified sites: `main.py:520` (main loop), `risk_manager.py:255` (`on_fill`), `strategies/market_maker.py:115`, `strategies/intra_arbitrage.py:149,164`, `execution/clob_executor.py:173,183`. |
| Recommended insertion point | `main.py:520-540` post-executor-result handling, OR `risk_manager.on_fill`. Same insertion point as existing `metrics_exporter.record_fill` is recommended — makes D.2.4 a third sink layered on the same trigger. |
| Wire message type | `FILL` per dashboard `WsMessageSchema`. Payload: `id`, `ts`, `strategyId`, `symbol`, `action` (`UP`/`DOWN`), `price`, `qty`, `status` (`filled`/`pending`), `pnl?`, `value?`. |
| BotState / runtime fields read | From `result`: `result.filled_size`, `result.fill_price`. From `intent` (`OrderIntent`): `intent.side`, `intent.strategy`, `intent.symbol`. |
| `config.*` attributes accessed | **None expected.** All payload fields come from `result` and `intent`. |
| Verified field name | **N/A — no `config.*` access planned.** If implementation discovers a need (e.g. `config.runtime.mode` to suppress broadcast in sim), add the row and re-verify. |

### 5.2 `ALERT` emit

| Field | Value |
|---|---|
| Trigger | Bot-internal alert generated. Central function: `utils/alerts.py:send_alert` (telegram + discord backends). Call sites: `main.py:240` (`maybe_alert_equity_drift`), `utils/wallet_reconcile_cli.py:156`, `risk_manager.py:63`, `utils/equity_reconciliation.py:219`. |
| Recommended insertion point | **Inside `utils/alerts.send_alert` itself.** Every existing call site automatically broadcasts to the dashboard with zero per-callsite changes. |
| Wire message type | `ALERT` per BOT_CONTRACT: `id`, `ts`, `severity` (`info`/`warning`/`critical`), `message`, `strategy?`. |
| BotState / runtime fields read | The args passed to `send_alert` (message text + level). Note: `api/state.py:269` already has `self.recent_alerts` (a deque) — confirm at source-time whether `send_alert` already appends there; D.2.4 broadcast becomes a second sink, not a replacement. |
| `config.*` attributes accessed | **Expected: none.** Severity is computed upstream (`maybe_alert_equity_drift` already classifies before calling `send_alert`). |
| Verified field name | **N/A — no `config.*` access planned.** If `send_alert` ends up reading `config.risk.daily_drawdown_limit_pct`, verified: `BotConfig.risk → RiskConfig.daily_drawdown_limit_pct` (type `float`, default 0.12, range 0.03–0.30). |

### 5.3 `STRATEGY_UPDATE` emit

| Field | Value |
|---|---|
| Trigger | Strategy runtime config changes via `POST /api/strategies/config` or internal state mutation. Verified path: `api/state.py:43-45` defaults (`StrategyRuntimeConfig(id="strategy1", enabled=True)` × 3); `tests/api/test_state.py:163,176,184` shows `StrategyConfigPatch` applied to `state.strategy_configs[<id>]`. |
| Recommended insertion point | **Inside the state-mutation method in `api/state.py` that applies `StrategyConfigPatch` to `state.strategy_configs[<id>]`.** Single central choke point, fires exactly when state actually changes. |
| Wire message type | `STRATEGY_UPDATE` per BOT_CONTRACT: partial strategy row keyed by `strategyId`. |
| BotState / runtime fields read | `state.strategy_configs[<id>]` — the just-updated `StrategyRuntimeConfig` (fields `id`, `enabled`, `maxPositionSize`, and whatever else the class carries — verify at source-time). |
| `config.*` attributes accessed | **Expected: none.** This is critical: the dashboard-facing strategy state is `state.strategy_configs[<id>]` (in `api/state.py`), NOT `config.strategy.*` (in `config/settings.py`). See §6.0. |
| Verified field name | **N/A — no `config.*` access planned.** Available on `BotConfig.strategy → StrategyConfig` if ever needed: `momentum_enabled`, `market_maker_enabled`, `smart_money_enabled`, `smart_money_min_win_rate`, `smart_money_min_profit_factor`, `min_liquidity_usd`, `min_arb_edge`, `arb_max_unwind_bps`, `momentum_z_threshold`, `mm_spread_bps`, `momentum_window`. |

## 6. Cross-cutting notes

### 6.0 Two strategy surfaces — do not confuse

Analogous to `config.api` vs `config.http_api`: there are two real strategy-related state surfaces in this codebase, and they are NOT the same.

| Surface | Path | Source | Lifecycle | Used by |
|---|---|---|---|---|
| Boot-time strategy flags | `config.strategy.*` | `config/settings.py:StrategyConfig` | Set at process start via `BOT_STRAT_*` env vars, immutable thereafter | Strategy init (e.g. `data_feeds/polygon_feed.py:90`, `main.py:486`) |
| Runtime strategy configs | `state.strategy_configs[<id>]` | `api/state.py:StrategyRuntimeConfig` | Mutable via `POST /api/strategies/config` | Dashboard, REST endpoints, D.2.4 STRATEGY_UPDATE emit |

D.2.4 STRATEGY_UPDATE emit reads the **runtime** surface. Mixing them up at an emit site would be the exact analog of the v0.2.7 wrong-real-field bug in the strategy domain.

### 6.1 Existing observability sinks D.2.4 layers on top of

| Event | Existing sink 1 | Existing sink 2 | New (D.2.4) |
|---|---|---|---|
| Fill | `risk_manager.on_fill` (state mutation) | `observability/metrics_exporter.record_fill` (Prometheus) | Dashboard WebSocket broadcast |
| Alert | `utils/alerts.send_alert` → Telegram/Discord | `api/state.recent_alerts` deque (snapshot-readable) | Dashboard WebSocket broadcast |
| Strategy update | `api/state.strategy_configs` mutation (snapshot-readable) | — | Dashboard WebSocket broadcast |

### 6.2 Sim-mode emit policy

Open question: should emit sites broadcast when `config.runtime.mode == "sim"`?

**Recommended: emit unconditionally; do not read `config.runtime.mode` at emit sites.** The dashboard's whole point is to visualize what the bot is doing, including simulated runs. Cross-contamination (sim broadcasts polluting a production dashboard view) only happens if you point both bots at the same WS URL, which is operator error. Filtering is a dashboard-side concern if needed later.

## 7. Gate-close checklist

- [x] §5.0 surface map filled against `config/settings.py`
- [x] §5.1 FILL emit site documented, no `config.*` access planned
- [x] §5.2 ALERT emit site documented, no `config.*` access planned
- [x] §5.3 STRATEGY_UPDATE emit site documented, no `config.*` access planned
- [x] §6.0 two-strategy-surface trap documented
- [x] §6.1 existing observability sinks recorded
- [x] Status line at top flipped to `GATE CLOSED — verified 2026-05-13 against config/settings.py at v0.2.7 (d39696a)`
- [x] **No `config.*` access in any planned emit site.** If this changes during implementation, source change does NOT land until §5 is re-verified and status flipped back to GATE OPEN.

## 8. Companion fixture hardening (tracked separately)

Per §4. Follow-up commit, not D.2.4-blocking. Migrate `tests/api/conftest.py` `bot_config` fixture to real-instance or per-sub-config-spec; add regression test asserting `isinstance(config.http_api, HttpApiConfig)` etc.

---

*Gate closed 2026-05-13. D.2.4 source changes may begin. If any emit-site source change adds a `config.*` access not documented in §5, this file must be reopened (status → GATE OPEN) and the new access verified before that source change lands.*

---

## 9. Step landings (filled as each step lands)

### 9.1 ALERT step — landed at SHA `4f63d9b`

Wire-up + envelope reconciliation per §10. No `config.*` access introduced
(audit §5.2 verified N/A). Emitter handoff: module-level singleton
(`_emitter`, `set_emitter`, `get_emitter` in `utils/alerts.py`), mirroring
`observability/metrics_exporter.py` precedent.

### 9.2 FILL step — *not yet landed*

### 9.3 STRATEGY_UPDATE step — *not yet landed*

---

## 10. Schema drift discovery + reconciliation

### Background

The audit gate closed on 2026-05-13 verifying only `config.*` access
patterns (§5). It did not verify the WS payload schema shapes against the
dashboard's zod contract, because §5.2 punted with "ALERT per BOT_CONTRACT"
without naming which side's contract was canonical when they disagreed.
No D.2.3 test ever built an ALERT-, FILL-, or TICK-shaped envelope; the
Emitter unit tests only used `{"type": "TEST"}`. The drift survived v0.2.7
release and was discovered at the start of D.2.4 step 1 (ALERT).

### What was wrong

Three envelope-shape bugs and one field-set bug.

**Envelope nesting:** the bot defined `WsTickPayload`, `WsFillPayload`, and
`WsAlertPayload` classes that each wrapped the inner type under a named
key (`.tick`, `.fill`, `.alert`). Wire shape would have been e.g.:

```json
{"type": "ALERT", "payload": {"alert": {<AlertItem fields>}}}
```

Dashboard `components/dashboard/schemas.ts:215` defines
`WsMessageSchema = z.discriminatedUnion("type", [ ..., z.object({ type:
z.literal("ALERT"), payload: AlertItemSchema }), ... ])`. Dashboard
`lib/applyWsMessage.ts:58` reads `message.payload` as the `AlertItem`
directly. So the dashboard expected flat:

```json
{"type": "ALERT", "payload": {<AlertItem fields>}}
```

Same flat shape for TICK and FILL. If the bot had broadcast through its
old envelopes, dashboard zod parsing would have silently dropped every
TICK / FILL / ALERT (no crash; `applyWsMessage` logs a warning and
discards). The droplet's D.2.3 smokes only exercised WS upgrade and ticket
auth, never an actual emit, so this never surfaced in production.

**`AlertItem` field-set drift:** bot had `ts: int` (epoch ms), no
`source`, plus extra fields `acknowledged: bool` and
`strategy: Optional[StrategyId]`, and a severity literal that included
`"error"` in addition to `"info"/"warning"/"critical"`. Dashboard
`AlertItemSchema` (line 163) has `time: z.string()`, `source: z.string()`,
no `acknowledged` or `strategy`, and severity narrowed to three values.

### Why the gate didn't catch this

Audit doc §5 verified only that planned emit-site code wouldn't read
`config.*` attributes in ways that mirrored the v0.2.7 `config.api` vs
`config.http_api` hotfix. The gate was effective for what it audited and
defended against a real class of bug. But the gate's scope was narrower
than the implicit assumption "if the gate is closed, ALERT is ready to
ship." Schema shape between bot and dashboard was not on the §5 audit
checklist, and the §5.2 row for ALERT explicitly deferred shape questions
("ALERT per BOT_CONTRACT") without resolving which side's contract held
authority on disagreement.

### Resolution

Dashboard schemas are canonical. Bot moves. In this batch:

- `api/schemas.py`: `WsTickPayload`, `WsFillPayload`, `WsAlertPayload`
  deleted. `WsTick.payload: PriceTick`, `WsFill.payload: FillRecord`,
  `WsAlert.payload: AlertItem`.
- `AlertItem` reshaped: `id: str`, `time: str` (replacing `ts: int`),
  `severity: Literal["info","warning","critical"]` (dropped `"error"`),
  `source: str` added, `message: str` retained. `acknowledged` and
  `strategy` fields dropped after grep confirmed no non-test consumers.
- `DashboardSnapshot.recentAlerts: Optional[list[AlertItem]]` continues
  to reference the (now-corrected) `AlertItem`. The REST snapshot's
  `recentAlerts` field shape is incidentally fixed by this reconciliation.
  The `api/routes_rest.py` blob `8ae836cf` remains frozen; only payload
  schemas in `api/schemas.py` evolved. The "frozen REST contract" anchor
  was about routes, not schemas.

### Open items deferred to other batches

- **WsStrategyUpdate envelope drift.** `WsStrategyUpdatePayload.strategies`
  + `.strategyConfigs` does not match dashboard's
  `StrategyUpdateSchema = z.object({ strategyId, updates })`. Will be
  reconciled in the same-commit-as-wire-up STRATEGY_UPDATE step batch.
- **WsFill envelope drift.** This batch flattens `WsFill.payload` to
  `FillRecord` (matching dashboard), but does NOT verify `FillRecord`
  field-by-field against dashboard `FillSchema`. That verification is
  part of the FILL step batch.
- **`AlertItem.acknowledged` / `.strategy` fields.** Dropped from the bot
  schema. If a future bot-side workflow needs alert-acknowledgement
  tracking or per-strategy alert attribution, dashboard schemas would
  need to gain those fields first; bot follows.
- **Schema-contract regression test.** A bot-side test asserting
  `api/schemas.py` envelope shapes match a frozen snapshot of dashboard
  `WsMessageSchema` would have caught this drift. Not added in this
  batch — queued as housekeeping.

### Process note

The §5 audit gate worked. The bug it didn't catch wasn't in its scope.
Future audit docs that gate cross-repo emit sites should add a "schema
shape verification" section parallel to the `config.*` access verification:
list every WS message type, name the canonical-side schema by file:line,
verify field-by-field equivalence between bot and dashboard before
declaring the gate closed.

### 9.2 FILL step — landed at SHA `<COMMIT-SHA-AFTER-COMMIT>`

Wire-up + envelope reconciliation + Level-1 `pnl`-nullable adoption per
§10 (FILL drift narrative). Emit site is `main.py:_exec_and_record`,
inserted as a fourth peer line after `notify_fill_metrics` and before
`record_fill_return_for_correlation` (matching the established
fire-and-forget-side-effect pattern at that scope). Emitter handoff
via module-level singleton in `utils/fills.py`, mirroring
`utils/alerts.py`.

No `config.*` access introduced (audit §5.1 verified N/A). RiskManager
constructor signature unchanged: the emit site lives in `main.py`, not
inside `risk.on_fill`, so no plumbing changes to risk_manager.py.

Five pre-existing `FillRecord(...)` construction sites in
`tests/api/conftest.py` and `tests/api/test_schemas.py` updated in the
same commit to the new shape. New regression test
`tests/api/test_schema_contract.py` covers ALERT + FILL envelope drift;
STRATEGY_UPDATE coverage deferred to step 3.

---

## 10 (continued). FILL step — drift discovery + Level-1 resolution

### Background

The §5.1 audit row for FILL named two candidate emit sites
(`main.py:520-540` post-executor-result, vs `risk_manager.on_fill`) but
did not decide between them. Phase 0 investigation at the start of the
FILL batch resolved the ambiguity: `notify_fill_metrics` already fires
at `main.py:516-527` as a peer to `risk.on_fill`, not from inside it.
The "Call once per fill after RiskManager.on_fill" comment in
`observability/metrics_exporter.py` describes *temporal ordering*
(after `risk.on_fill` returns), not *call site*. FILL broadcast lands as
the fourth peer at that same scope.

### Schema drift (worse than ALERT)

Bot `FillRecord` and dashboard `FillSchema` barely overlapped:

| Bot `FillRecord` (pre-batch) | Dashboard `FillSchema` (canonical) | Resolution |
|---|---|---|
| `id` | `id` | match |
| `marketId` | — | dropped (0 non-test consumers) |
| `outcome: "YES"\|"NO"` | — | dropped (0 non-test consumers) |
| `action: "UP"\|"DOWN"` | `action: enum("UP","DOWN")` | match |
| `quantity: float` | `size: number` | renamed quantity → size |
| `price: float` | `price: number` | match |
| `strategy: StrategyId` | `strategy: string` | match |
| `ts: int` (epoch ms) | `time: string` (HH:MM:SS) | type+name change, same as ALERT |
| `fees: Optional[float]` | — | dropped (0 non-test consumers) |
| `pnl: Optional[float]` | `pnl: number` | **Level-1 escalation** (see below) |
| — | `symbol: string` | added; display-only mapping to `market_id[:12]` |
| — | `status: enum("filled","pending")` | added; hardcoded "filled" at emit site by guard-construction |

Three bot-side fields dropped (`marketId`, `outcome`, `fees`) had zero
non-test consumers verified by grep, matching the ALERT precedent for
field-drops. Three new fields added with documented defaults: `symbol`
is `market_id[:12]` (consistent with how the bot already logs market
IDs at `main.py:502`), `status` is hardcoded `"filled"` (guard at
`main.py:516` only emits when `result.accepted and result.filled_size
> 0`), `pnl` see below.

### The `pnl` Level-1 escalation

Dashboard `FillSchema.pnl: z.number()` requires the bot to know
realized pnl at fill time. The bot does NOT know realized pnl at fill
time — pnl is computed downstream in equity reconciliation, not in
`_exec_and_record`. Three options were considered:

1. **Default `pnl=0.0` at emit.** Rejected as actively misleading: the
   dashboard would render "$0.00 pnl" for every real-time fill,
   indistinguishable from a real $0 pnl trade. Worse than "no
   information": it presents wrong information as if it were correct.

2. **Compute `pnl` inline at the emit site from `fill_price ×
   filled_size - cost_basis`.** Rejected because `cost_basis` (entry
   price for the position) isn't tracked in `TradeResult` and isn't
   trivially derivable. Computing nonsense from incomplete inputs.

3. **Bot emits `pnl=None`, dashboard schema becomes
   `pnl: z.number().nullable()`, dashboard renders "—" or "(pending)"
   for null.** Adopted. This is the honest representation of "we don't
   know yet; downstream will know."

This is the first place in the bot/dashboard reconciliation where the
"dashboard canonical" rule did not hold. The rule's justification is
that the dashboard schemas have received careful review and represent
the contract a human would consciously design. For `pnl` at fill time,
the dashboard schema is wrong: it asserts knowledge the bot
genuinely does not possess. Adopting the dashboard schema here would
force the bot to lie (default to 0.0 or compute nonsense). The bot
keeps `Optional[float]` and emits `None`; the dashboard side gains
`.nullable()` in a paired commit.

This pattern — bot field's optionality reflecting a real epistemic gap,
dashboard schema needing to accommodate — is generalisable. Future
batches should default to "dashboard canonical" but escalate when the
schema asserts information the bot can't produce.

### Schema-contract regression test landed

`tests/api/test_schema_contract.py` (new) snapshots dashboard field
expectations for `AlertItem` and `FillRecord` plus envelope-flatness
invariants for `WsAlert` and `WsFill`. The test was scoped narrower
than ratified — field-set comparison rather than full JSON-schema
output comparison — to avoid brittle exact-match against pydantic's
output format. The drift class this guards against is field
addition/deletion and envelope re-nesting; full type compatibility is
verified by the pydantic-validate round-trip tests in
`tests/utils/test_fills.py` and `tests/utils/test_alerts.py`.

STRATEGY_UPDATE coverage will be added to this file when
`WsStrategyUpdatePayload` is reconciled.

### Dashboard-side companion changes (separate commit)

- `components/dashboard/schemas.ts:73`: `pnl: z.number()` →
  `pnl: z.number().nullable()`
- Fills-list render component: show "—" (em-dash) for null pnl

### Process note (added to the ALERT process note)

"Dashboard canonical" is a working principle, not an inviolable rule.
Escalate to Level-1 review when the dashboard schema asserts
information the bot does not possess at the emit site.
