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

### 6.0 Three strategy surfaces — do not confuse

Analogous to `config.api` vs `config.http_api`: there are **three** real strategy-related state surfaces in this codebase, and they are NOT the same.

| Surface | Path | Source | Lifecycle | Used by |
|---|---|---|---|---|
| (1) Boot-time strategy flags | `config.strategy.*` | `config/settings.py:StrategyConfig` | Set at process start via `BOT_STRAT_*` env vars, immutable thereafter | Strategy init (e.g. `data_feeds/polygon_feed.py:90`, `main.py:486`) |
| (2) Runtime strategy configs | `state.strategy_configs[<id>]` | `api/state.py:StrategyRuntimeConfig` | Mutable via `POST /api/strategies/config` | Dashboard config form (REST + snapshot hydration), REST endpoints |
| (3) Card telemetry (display) | `StrategyCardData` partial — the `STRATEGY_UPDATE.updates` payload per `BOT_CONTRACT.md:413, 609` | computed downstream of trades/PnL by future strategy-telemetry pipeline (v0.2.9+) | Streamed as deltas via WS `STRATEGY_UPDATE` messages | Dashboard strategy card UI rendering only |

**STRATEGY_UPDATE on the WS contract is for surface (3) — card telemetry — NOT surface (2).** The dashboard's `applyWsMessage.ts` reducer spreads `STRATEGY_UPDATE.updates` onto a strategy ROW (a `StrategyCardData`), not onto `strategyConfigs`. Per `BOT_CONTRACT.md:437`: *"This message does not update strategyConfigs in the store — only card fields. Config changes from the form rely on REST and snapshot hydration."*

The v0.2.8 d1d2276 wire-up at `apply_strategy_patch` confused surface (2) for surface (3): it emitted `StrategyConfigPatch` fields (`enabled`, `maxPositionSize`, `riskBudget`, `activePairs`) where the contract expects `StrategyCardData` partial keys (`status`, `pnlToday`, etc.). The dashboard's zod `.partial()` schema silently stripped all four unknown keys, the reducer applied an empty delta, and the card UI did not update. Wire-valid, contract-nonconformant, functional no-op. Reverted post-tag-move; see §10 (correction block). Future emit-site work touching "anything strategy-related" must explicitly identify which of the three surfaces it is emitting from.

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

### 9.3 STRATEGY_UPDATE step — initially landed at `d1d2276`; partially reverted in v0.2.8 post-tag correction

**Status (after v0.2.8 post-tag correction):**

**Landed and KEPT:**
- `StrategyCardData` reshape in `api/schemas.py` to match `BOT_CONTRACT.md:113` (status `live`|`paused`, 7 nullable telemetry fields, `sparkline: list[float]` default-empty). Snapshot construction in `api/routes_rest.py:_build_snapshot` simplified to match.
- Envelope flattening: `WsStrategyUpdate.payload` is `StrategyUpdate` directly (no `WsStrategyUpdatePayload` wrapper), parallel to ALERT/FILL.
- Broadcast infrastructure in `utils/strategy_updates.py` (sync-from-async bridge via `asyncio.get_running_loop().create_task`, module-level emitter singleton). Mechanically correct; awaiting a correct caller.
- Schema-contract regression test in `tests/api/test_schema_contract.py` for envelope-flatness invariant.
- Unit tests in `tests/utils/test_strategy_updates.py` (5 scenarios: registered/unregistered, sync-context-no-loop, multi-call, pydantic round-trip with explicit field-readback).

**Landed and REVERTED (post-tag-move):**
- Wire-up call from `api/state.py:BotState.apply_strategy_patch` to `broadcast_strategy_update`. The emission carried `StrategyConfigPatch` fields (`enabled`, `maxPositionSize`, `riskBudget`, `activePairs`) — surface (2) of the §6.0 three-surface model. `BOT_CONTRACT.md:413, 609` specifies `STRATEGY_UPDATE.updates` as a partial of `StrategyCardData` — surface (3). Empirically confirmed silent no-op: zod's `.partial()` stripped all four unknown keys; dashboard reducer applied `{}` delta. See §10 correction block.
- `StrategyUpdateFields` reshaped from 4-field `StrategyConfigPatch` mirror to 11-field partial-of-`StrategyCardData` (contract-correct, but no current caller — ready for v0.2.9).
- `_set_strategy_updates_emitter` registration in `api/server.py` (no caller, so no need to register).
- `tests/api/test_schema_contract.py` STRATEGY_UPDATE field-set test renamed and rewritten to assert against the **literal contract text** (`BOT_CONTRACT.md:609`), not against the bot's own pydantic model. Discipline lesson: a regression test that asserts bot-against-bot proves nothing — it's self-consistent with whatever the bot emits, including the wrong thing.

**Correct emit site is deferred to v0.2.9.** The legitimate source of `StrategyCardData` deltas is the future strategy-telemetry pipeline (which today does not exist; that's part of why d1d2276 wired against the only available surface, which was the wrong one). Once telemetry computes per-strategy `pnlToday`/`tradesToday`/etc deltas, those values feed `broadcast_strategy_update` directly. The mechanism is ready.

The §6.0 three-surface model was expanded in this correction (previously "two surfaces") to make the trap explicit for future emit-site work.

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

### 9.2 FILL step — landed at SHA `a98f3ea`

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

STRATEGY_UPDATE coverage was added in step 3 (see appended §10
STRATEGY_UPDATE narrative at end of this file) with field-set snapshot
of `StrategyUpdateFields` + envelope-flatness invariant for
`WsStrategyUpdate`. The wrapper class `WsStrategyUpdatePayload` was
dropped at the same time, parallelling the ALERT/FILL flattening.

### Dashboard-side companion changes (separate commit)

- `components/dashboard/schemas.ts:73`: `pnl: z.number()` →
  `pnl: z.number().nullable()`
- Fills-list render component: show "—" (em-dash) for null pnl

### Process note (added to the ALERT process note)

"Dashboard canonical" is a working principle, not an inviolable rule.
Escalate to Level-1 review when the dashboard schema asserts
information the bot does not possess at the emit site.


---

## 10 (continued, again). STRATEGY_UPDATE step — drift + bot-narrower escalation

> **2026-05-18 correction.** The "Resolution — permissive superset, but
> semantic not syntactic" subsection below and item 2 of the "Process
> note" at the end of this section are WRONG. Re-reading `BOT_CONTRACT.md:413`
> and `:609` confirms `STRATEGY_UPDATE.updates` MUST be a partial of
> `StrategyCardDataSchema` (card-telemetry display fields), NOT of
> `StrategyConfigPatch` (runtime-config fields). The two field-sets
> are disjoint at the field-name level.
>
> The bot's d1d2276 emission of `{enabled, maxPositionSize, riskBudget,
> activePairs}` is wire-valid (the dashboard's zod `.partial()` accepts
> unknown keys but strips them silently) but **semantically empty**
> after parse: the resulting `updates` dict is `{}` and the reducer
> spread `{...row, ...{}}` is a no-op. Empirically verified by
> reconstructing the dashboard's exact zod schemas + reducer step from
> `components/dashboard/schemas.ts` at b26eb3d and running the bot's
> emission through them: parse success, parsed.data.updates `{}`,
> row-before == row-after.
>
> **The `apply_strategy_patch` site is NOT a contract-conformant emit
> site for STRATEGY_UPDATE.** The legitimate emit site for card-data
> deltas is the future per-strategy telemetry pipeline (whichever
> module produces `StrategyCardData` updates downstream — does not
> exist as of v0.2.8). The v0.2.8 wire-up was REVERTED post-tag-move.
> See §6.0 (now "three strategy surfaces" — the misread was treating
> surface (2) runtime-config as the emit source where the contract
> wants surface (3) card-telemetry).
>
> **What from §10's STRATEGY_UPDATE narrative remains correct:**
> - The schema-drift table showing disjoint bot/dashboard field-name sets — that observation is factually accurate.
> - The `sparkline` Gate 1 sub-decision (empty list `[]` vs `null`) — still the right call.
> - The sync-broadcast-from-sync-state-method engineering detail (the bridge mechanism in `utils/strategy_updates.py`) — mechanically sound.
> - The `StrategyCardData` reshape table — matches `BOT_CONTRACT.md:113` exactly.
>
> **What from §10's STRATEGY_UPDATE narrative is WRONG:**
> - The "Resolution — permissive superset, but semantic not syntactic" framing. The narrative reasoned that the bot's disjoint emission was OK because zod silently drops unknown keys. It is not OK: silent drop = no-op = card never updates.
> - The "STRATEGY_UPDATE-style escalation pattern" in the Process note (item 2 at the end of this section). That is not an escalation pattern; it is a misread of the contract.
>
> Treat the historical narrative below as a record of how the bug
> got shipped, not as a current design rationale. The functional
> outcome of v0.2.8 BEFORE the correction was that dashboard strategy
> cards never received per-key telemetry deltas via the WS path; with
> the correction (revert), they remain on the snapshot-hydration path
> (initial `_build_snapshot` + future v0.2.9 telemetry pipeline).

### Background

The §5.3 audit row for STRATEGY_UPDATE named exactly one emit-site
candidate: `api/state.py:BotState.apply_strategy_patch`, the single
choke-point where strategy runtime config mutates. No ambiguity to
resolve (unlike FILL's two-candidate situation). What was *not*
foreseen by the audit was the depth of the field-name mismatch
between the bot's STRATEGY_UPDATE payload and the dashboard's
`StrategyUpdateSchema`. The ALERT batch had partial overlap and a
clean rename path; the FILL batch had wider drift plus the `pnl`
Level-1 escalation; STRATEGY_UPDATE has effectively **no overlap** at
the field-name level, and the resolution required a different kind of
escalation than FILL's.

### Schema drift — disjoint at the field-name level

The bot's pre-batch `WsStrategyUpdatePayload` was a thin wrapper
around `StrategyRuntimeConfig` deltas. The four fields it carries are
exactly the four fields a `StrategyConfigPatch` can mutate:
`enabled`, `maxPositionSize`, `riskBudget`, `activePairs`.

The dashboard's `StrategyUpdateSchema` is a different shape entirely.
It's defined as `StrategyCardDataSchema.omit({id: true}).partial()` —
i.e. a partial-of-all-card-data-fields. The card data schema lists
display-time fields the dashboard renders in the strategy card grid:
`name`, `status`, `pnlToday`, `pnlMonth`, `winRate`, `tradesToday`,
`avgHoldMinutes`, `lastSignal`, `pipelineStep`, `exposure`, `sparkline`.

| Bot `StrategyUpdateFields` (post-batch) | Dashboard `StrategyCardUpdatesSchema` | Overlap |
|---|---|---|
| `enabled: Optional[bool]` | — | no field-name overlap |
| `maxPositionSize: Optional[float]` | — | no field-name overlap |
| `riskBudget: Optional[float]` | — | no field-name overlap |
| `activePairs: Optional[str]` | — | no field-name overlap |
| — | `name`, `status`, `pnlToday`, `pnlMonth`, `winRate`, `tradesToday`, `avgHoldMinutes`, `lastSignal`, `pipelineStep`, `exposure`, `sparkline` | — |

Two genuinely different things are competing for the same WsMessage
discriminant. The bot is emitting *runtime-config patches* (what
changed in the persistent config). The dashboard is consuming
*card-data partials* (what to repaint in which cell). Same envelope
type, different semantic content.

### Resolution — "permissive superset", but semantic not syntactic

Three options were considered for the bot side:

1. **Bot widens to match the dashboard card-data partial.** Rejected.
   The bot does not have `pnlToday`/`pnlMonth`/`winRate`/etc to emit
   from `apply_strategy_patch`; those are computed downstream in the
   strategy-loop's metrics pipeline. Emitting them from
   `apply_strategy_patch` would be inventing fields the bot does not
   know — the same trap §10's FILL `pnl` escalation already named.

2. **Bot keeps narrow, dashboard narrows too.** Rejected. The
   dashboard *does* receive card-data partials at runtime (from the
   metrics pipeline, eventually). Narrowing the dashboard schema to
   match the bot's four-field patch would break the legitimate
   card-data-partial wire path.

3. **Bot keeps narrow (four runtime-config fields), dashboard keeps
   wide (eleven card-data fields), discriminant union accepts both
   shapes vacuously via zod's partial-with-unknown-keys behaviour.**
   Adopted. The bot emits fields the dashboard treats as unknown via
   zod's lenient parsing on `.partial()` schemas (unknown keys are
   dropped, not rejected). The dashboard accepts a payload it
   doesn't render any card cells from — that's fine, because the
   `applyWsMessage` reducer at `lib/applyWsMessage.ts:69-72`
   does a per-key delta-merge and simply skips bot-supplied keys it
   doesn't have card cells for.

This is a different shape of escalation than the FILL `pnl` case. FILL
was "bot field's optionality reflects a real epistemic gap"; STRATEGY_UPDATE
is **"bot's field-set and dashboard's field-set are intentionally
disjoint at the field-name level; the wire compatibility is
*semantic* rather than *syntactic*."** The schema-contract regression
test (`tests/api/test_schema_contract.py`) documents this asymmetry
explicitly and snapshots the bot's narrow four-field shape positively
rather than asserting a strict subset relation that doesn't actually
hold at the field-name level.

### The `sparkline` Gate 1 sub-decision

`StrategyCardData.sparkline: list[float] = Field(default_factory=list)`.
The natural alternative was `Optional[list[float]] = None` — but those
two emit different wire values (`[]` vs `null`), and the dashboard's
sparkline rendering branch treats them differently. An empty
time-series (`[]`) means "no points yet, show the empty axis";
`null` would mean "no series at all, hide the panel." For initial
snapshot construction, the bot has *zero points* but the series
*exists*, so `[]` is the honest signal. This is distinct from the
nullable scalar telemetry fields (`pnlToday: Optional[float] = None`,
etc.) where "we don't have a number yet" really is structurally
different from "the number is zero." A list-typed time series with
no points is not the same kind of absence — empty is the right
default.

### Sync-broadcast-from-sync-state-method engineering detail

`apply_strategy_patch` is sync (mirrors `apply_patch` on
`StrategyRuntimeConfig`, called from sync test setup as well as
async HTTP route handlers). `Emitter.broadcast` is async. The bridge:
`utils/strategy_updates.broadcast_strategy_update` is a SYNC function
that probes for a running event loop with
`asyncio.get_running_loop()`. If a loop is found, the broadcast is
scheduled via `loop.create_task(em.broadcast(message))` —
fire-and-forget, parallelling `utils/alerts.send_alert` semantics.
If `RuntimeError` is raised (no running loop), the broadcast silently
no-ops. That `RuntimeError`-as-control-flow is intentional: it lets
sync state-mutation tests run without needing to spin up an event
loop just to suppress the broadcast.

The test surface locks this in: three sync `apply_patch` tests in
`tests/api/test_state.py` install a mock emitter and assert
`em.broadcast.await_count == 0` (no broadcast in sync test context),
while a fourth `@pytest.mark.asyncio` test asserts
`em.broadcast.await_count == 1` with the correct delta payload (sync
state mutation triggers async broadcast when a loop IS available).

### Schema-contract regression test extended

`tests/api/test_schema_contract.py` (already established in step 2)
gained three STRATEGY_UPDATE assertions:

1. `test_strategy_update_fields_match_bot_runtime_patch_shape` —
   positive snapshot of the bot's exact four-field set
   (`enabled`/`maxPositionSize`/`riskBudget`/`activePairs`). Drift
   trips this loudly.
2. `test_strategy_update_payload_envelope_shape` — asserts
   `StrategyUpdate` is the flat `{strategyId, updates: StrategyUpdateFields}`
   shape and that the `updates` annotation is the direct class
   reference (not a `ForwardRef`). pydantic v2 lazy forward-ref
   resolution surfaced here during phase 1: classes are now ordered
   so `StrategyUpdateFields` is defined *before* `StrategyUpdate` and
   no `model_rebuild()` is needed.
3. `test_ws_strategy_update_envelope_is_flat` — asserts
   `WsStrategyUpdate.payload` is `StrategyUpdate` directly (no
   re-introduced `WsStrategyUpdatePayload` wrapper class).

A hardcoded snapshot of the dashboard's eleven-field
`StrategyCardUpdatesSchema` is kept in the test as a comment-block
constant ~~`_DASH_STRATEGY_CARD_UPDATES_FIELDS`~~ (**renamed
2026-05-18 to `_CONTRACT_STRATEGY_UPDATE_FIELDS`** — see top-of-section
correction block). ~~The accompanying prose comment explicitly
documents that the "permissive superset" claim is *not* asserted as
a strict subset relation, because the relation is disjoint at the
field-name level.~~ **STRUCK 2026-05-18.** Post-correction, the test
asserts the bot's `StrategyUpdateFields` field-set MATCHES the
contract literal exactly — not as a subset, as an equality. The
"disjoint at field-name level" claim was true of the bot's d1d2276
emission against the contract; that disjoint-ness was the bug, not a
designed asymmetry.

### `StrategyCardData` was reshaped in the same batch

`api/schemas.py:StrategyCardData` (the row shape carried by snapshot
GET, separate from the WS update payload) was reshaped to match the
dashboard's `StrategyCardDataSchema`:

| Bot pre-batch | Bot post-batch | Dashboard |
|---|---|---|
| `status: Literal["active","paused"]` | `status: Literal["live","paused"]` | `enum("live","paused")` |
| `pnl`, `pnlPct`, `trades` (floats) | dropped | dropped |
| — | `pnlToday`, `pnlMonth`, `winRate`, `tradesToday`, `avgHoldMinutes`, `lastSignal`, `exposure` (all `Optional`) | matching nullable schema |
| `pipelineStep: int` | `pipelineStep: int` | match |
| — | `sparkline: list[float] = Field(default_factory=list)` | match (empty list, not null — see Gate 1 note above) |

The snapshot construction at `api/routes_rest.py:76-84` was
simplified: status is `"live" if cfg.enabled else "paused"`, and
all telemetry defaults fall through pydantic field defaults. The
existing test_routes_rest.py snapshot assertion was updated
`"active"` → `"live"`.

### Dashboard-side companion changes (separate commit)

- `components/dashboard/schemas.ts:StrategyCardDataSchema.status` enum
  is already `["live","paused"]` — no change needed (the drift was
  bot-side).
- `lib/applyWsMessage.ts:69-72` already delta-merges unknown keys
  silently; no change needed.
- A `dashboard/docs/audit/reviewer-pattern.md` ORCHESTRATOR-APPROVE
  doc-only commit lands in parallel with the bot commit, recording
  that the dashboard side is unaffected by the bot's narrow emission
  ~~(semantic superset confirmed by code-read, no runtime change)~~.
  **STRUCK 2026-05-18.** The "no runtime change" claim was wrong: the
  dashboard *was* affected — the bot's narrow emission resulted in
  zod stripping all four unknown keys and the reducer applying a `{}`
  no-op delta to the strategy card row. The card did not update. The
  6a6774e dashboard companion commit's actual value was in the
  *snapshot path* (nullable telemetry fields + null-safe rendering),
  not in the WS path. See top-of-section correction block.

### Process note (added to the FILL process note)

Two escalation patterns exist (per the v0.2.8 post-tag correction):

1. **FILL-style** — bot field's optionality reflects a real epistemic
   gap (`pnl` at fill time); dashboard schema becomes `.nullable()`.
   This pattern is legitimate; the bot genuinely cannot supply the
   value at emit time, and a nullable contract is the honest design.

2. ~~STRATEGY_UPDATE-style — bot's emit-site domain (runtime-config
   patches) and dashboard's consumer domain (card-data partials) are
   genuinely different... wire compatibility is semantic rather than
   syntactic~~ — **STRUCK 2026-05-18.** This was a misread of the
   contract. `STRATEGY_UPDATE.updates` is unambiguously a partial of
   `StrategyCardData` per `BOT_CONTRACT.md:413, 609`. Asserting "semantic
   compatibility" because zod silently drops unknown keys is rationalizing
   a silent no-op. The correct response when the bot's emit-site
   domain and the contract's payload domain disagree is to find the
   right emit site (or admit it does not exist yet and defer wiring),
   not to ship a wire-valid no-op and call it semantic compatibility.

**Process note for contract-regression test discipline (v0.2.9+):**
The schema-contract regression test in `tests/api/test_schema_contract.py`
was supposed to catch the STRATEGY_UPDATE drift. It did not, because
the original test snapshotted the bot's own pydantic model and
asserted bot-against-bot. That is self-consistent with whatever the
bot emits — including the wrong thing. The post-tag correction
re-grounds the snapshot against the LITERAL contract text in
`BOT_CONTRACT.md:609` (the constant `_CONTRACT_STRATEGY_UPDATE_FIELDS`
is copy-pasted from there). For future contract-regression work:
**always assert against the contract literal, never against the bot's
own model.** Using the bot model to verify itself proves nothing.
