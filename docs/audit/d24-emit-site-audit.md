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
