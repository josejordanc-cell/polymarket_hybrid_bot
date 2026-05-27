# D.2.1 — Bot HTTP/WebSocket Module Architecture

**Phase:** D.2.1 (architect)
**Author:** Team B architect (swarm session 2026-05-12)
**Status:** Draft — awaiting human review and commit
**Contract reference:** `docs/BOT_CONTRACT.md` v1.0
**Implements:** Three REST endpoints + one WebSocket server expected by the crypto-trading-bot-dashboard.

---

> ⚠️ **READER NOTE (2026-05-27 annotation).** This is the **D.2.1 forward-looking design artifact** written 2026-05-12 before D.2.2/D.2.3/D.2.4 implementation. Most "will" / "must" / "D.2.2 to enumerate" prose was correct as design intent, but the implementation diverged in several details — most notably the `BOT_HTTP_API_*` env-var prefix (vs the doc's proposed `BOT_API_*`) and the `config.http_api.*` config namespace (vs the doc's proposed `config.api.*`). Both names were renamed during D.2.2/D.2.3; the rename was load-bearing enough to cause a production 401 incident closed by hotfix `7c0e313` + `d39696a`. The seven actively-misleading factual errors flagged by the 2026-05-27 audit are corrected inline below; the remaining future-tense framing is preserved as historical design context. **For current-state references** (real env vars, real config paths, real route list, current schema field shapes) consult: `config/settings.py:239` for `HttpApiConfig`; `api/auth.py:115,179` for `verify_bearer` / `verify_ws_ticket`; `api/routes_rest.py:884-921` for the live route table; `api/routes_ws.py:9-76` for the real WS auth wire format (`?ticket=<jwt>`, NOT `?token=`); `api/state.py:107-160` for the current `BotState` field list; CHANGELOG `[Unreleased]` D.2.x entries; `README.md:15`; and `SECURITY.md` "Debug-injection endpoints". OQ-1 / OQ-2 / OQ-3 / OQ-4 open-question framings are mostly resolved — annotations below mark which.

---

## 1. Summary

This module adds an HTTP/WebSocket server to the `polymarket_hybrid_bot` that satisfies the integration contract defined in `docs/BOT_CONTRACT.md` v1.0. The server is built on **`aiohttp.web`** (server-side), which is already present as a direct dependency (`aiohttp==3.10.10` in `requirements.in`), adding zero new packages to the bot's dependency surface. The server runs as an additional `asyncio.create_task()` inside the existing `run_bot()` task graph in `main.py`, sharing the same event loop as all strategy, feed, and reconciliation tasks. It is placed under a new **`api/`** directory at the repo root, consistent with the project's flat top-level module layout (`strategies/`, `data_feeds/`, `execution/`, `observability/`). State is shared between the strategy loop and the HTTP module via a single `BotState` dataclass instance passed by reference at startup; the API layer reads from it, and the strategy loop writes to it. A bounded `asyncio.Queue` per WebSocket client handles back-pressure for streamed events.

---

## 2. Decisions D-1 through D-6

### D-1: Framework choice — `aiohttp.web` (not FastAPI)

**Choice: `aiohttp.web`.**

The bot's data feeds (`data_feeds/spot_feed.py`, `data_feeds/polymarket_feed.py`) already use `aiohttp` for HTTP client work. The same package exposes a full-featured HTTP server via `aiohttp.web`. Importing `aiohttp.web` for the server adds **zero new entries** to `requirements.in` or `requirements.txt` — the package is already pinned at `aiohttp==3.10.10`.

FastAPI would add two new direct dependencies (`fastapi`, `uvicorn`) plus Starlette's transitive chain. Those packages are not inherently risky, but introducing them into a dependency set that is already carefully version-pinned (CLAUDE.md hard rule 7 — eth-stack pins are load-bearing) creates unnecessary friction. FastAPI's automatic Pydantic request/response validation is useful, but the bot already uses Pydantic (`pydantic>=2.6.0,<3` in `requirements.in`) and calling `Model(**await request.json())` in aiohttp handlers is one line. The added validation expressiveness does not justify a new dependency.

`aiohttp.web` provides first-class WebSocket support via `aiohttp.web.WebSocketResponse`, which handles the upgrade, frame framing, and ping/pong at the transport level. This maps directly to the contract's requirements (PING/PONG heartbeat, close codes 4001/4002/4003). FastAPI's WebSocket support is via Starlette and works equally well, but again requires the additional packages.

**Decision rationale in one sentence:** `aiohttp.web` satisfies all contract requirements at zero additional dependency cost because `aiohttp` is already in the bot's runtime.

---

### D-2: Module placement and file layout

**Choice: `api/` at repo root.**

The project's existing modules all live at repo root: `strategies/`, `data_feeds/`, `execution/`, `observability/`, `utils/`, `config/`. Placing the new module at `polymarket_hybrid_bot/api/` would be inconsistent with this convention and would require relative-import adjustments across the codebase. Placing it at root as `api/` is both consistent and discoverable.

**Proposed file layout:**

```
api/
    __init__.py
    server.py
    routes_rest.py
    routes_ws.py
    auth.py
    state.py
    schemas.py
    emit.py
```

**File descriptions:**

#### `api/__init__.py`

Empty package marker. Exposes nothing at the package level; all public symbols are exported from `api/server.py`.

Imports: nothing.
Exposes: nothing.

#### `api/server.py`

The HTTP/WebSocket server entry-point. Owns the `aiohttp.web.Application` instance, registers routes from `routes_rest` and `routes_ws`, and exposes a single coroutine `run(state: BotState, shutdown: asyncio.Event) -> None` that is called via `asyncio.create_task()` from `main.py:run_bot()`.

Starts the server on startup, waits for `shutdown` to be set, then calls `runner.cleanup()` for graceful teardown. Does not manage the port or bind address directly — those are passed in from `config` via the `BotState` or explicitly from the call site in `main.py`.

Imports: `aiohttp.web`, `api.routes_rest`, `api.routes_ws`, `api.state.BotState`, `asyncio`, `structlog`.
Exposes: `run(state, shutdown) -> None`.

#### `api/routes_rest.py`

Implements the REST endpoint handlers. **As shipped (`api/routes_rest.py:884-921`), 7 routes are registered, not 3** — the design listed the 3 contract endpoints below, and v0.2.9 (3h + b9-fix + B.8) added 4 sim-mode-only `/debug/*` endpoints for runtime smoke. The four debug endpoints are 404-gated by `_verify_debug_mode` BEFORE the Bearer auth check, so non-sim deployments don't acknowledge them.

Contract endpoints (always live):

- `GET /dashboard/snapshot` — reads `state` and serialises a `DashboardSnapshotSchema`-compatible JSON response.
- `POST /api/strategies/config` — validates the request body against `StrategyConfigPatchSchema`, applies the change to `state.strategy_configs`, triggers `emit.strategy_update()` to push a `STRATEGY_UPDATE` WS message to all connected clients.
- `POST /emergency-stop` — sets `state.kill_switch_requested = True` (the strategy loop observes this flag on its next iteration and activates the kill switch), emits `STRATEGY_UPDATE` for all strategies showing `status: "paused"`, returns `{"ok": true, "stoppedAt": <epoch_ms>}`.

Debug endpoints (sim-mode only; 404 in live mode before auth check):

- `POST /debug/inject-position` — synthetic position injection for snapshot smoke (v0.2.9 3h).
- `POST /debug/clear-positions` — clears injected positions (v0.2.9 3h).
- `POST /debug/inject-alert` — exercises the full B.9 alert path (v0.2.9 b9-fix).
- `POST /debug/inject-log` — exercises the B.8 log path with 3f Option C lossy-level-map applied (v0.2.9 B.8).

All handlers call `api.auth.verify_bearer(request)` before doing anything else; return **401** on failure. Debug handlers additionally self-gate via `_verify_debug_mode` (returns 404 in non-sim mode); see `SECURITY.md` "Debug-injection endpoints".

Imports: `aiohttp.web`, `api.auth`, `api.state.BotState`, `api.schemas`, `api.emit`, `time`, `structlog`.
Exposes: `setup_routes(app, state, config)` — registers all 7 routes on the `aiohttp.web.Application`.

#### `api/routes_ws.py`

Implements the WebSocket server handler at `/ws`.

On new connection: reads `?ticket=` query param (NOT `?token=` as the design originally proposed — see `api/routes_ws.py:76` `request.query.get("ticket")`), calls `api.auth.verify_ws_ticket(ticket)`. On failure, closes with code **4001**. On success, creates a per-client `asyncio.Queue(maxsize=1000)`, adds it to a global subscriber registry in `state.ws_subscribers`, then enters the main receive/send loop. Responds to `{"type":"PING"}` with `{"type":"PONG"}`. On client disconnect or cancellation, removes the queue from the registry and closes cleanly.

The send loop is a separate coroutine that drains the per-client queue and sends JSON-serialised messages. Overflow is handled by the queue's `put_nowait` with try/except `asyncio.QueueFull` in `emit.py` (see D-5).

Imports: `aiohttp.web`, `api.auth`, `api.state.BotState`, `asyncio`, `json`, `structlog`.
Exposes: `websocket_handler(request)` — an `aiohttp.web` view function registered at `/ws`.

#### `api/auth.py`

Two public functions:

- `verify_bearer(request: aiohttp.web.Request) -> bool` — checks `Authorization: Bearer <token>` against `config.http_api.bot_api_token` (a `SecretStr` field in `config/settings.py:239` `HttpApiConfig`; the design originally proposed `config.api.bot_api_token` but D.2.3 hotfix `7c0e313` realigned to `config.http_api.*` after a production 401 incident). Returns `True` on match, `False` otherwise.
- `verify_ws_ticket(ticket: str) -> bool` — verifies an HS256 JWT using `config.http_api.ws_ticket_secret` (a `SecretStr` field in `config/settings.py:239`). Checks signature, algorithm (`HS256` only — reject `none` and RS256), and `exp`. Optionally tracks consumed `sub` (UUID) in a bounded in-memory `dict[str, float]` (sub → expiry) to implement single-use semantics; evicts entries whose `exp` has passed on each access to avoid unbounded growth. Returns `True` on valid-and-not-consumed ticket, `False` otherwise.

JWT verification requires `PyJWT>=2.8.0`. This package is **not yet in `requirements.in`**. It must be added before D.2.2 begins. See Open Questions.

Imports: `aiohttp.web`, `jwt` (PyJWT), `config.settings.BotConfig`, `time`, `structlog`.
Exposes: `verify_bearer`, `verify_ws_ticket`.

**Security note:** `verify_bearer` must call `.get_secret_value()` on the `SecretStr` at the point of comparison and must not log the token. Same for `config.http_api.ws_ticket_secret` in `verify_ws_ticket`. This is consistent with the existing secret-handling pattern throughout the codebase (`CLAUDE.md` hard rule 3).

#### `api/state.py`

Defines `BotState` — a single shared-state object instantiated once in `run_bot()` before the task graph starts, then passed into both the strategy loop (writer) and the API task (reader).

At a high level (exact fields enumerated by D.2.2 against `DashboardSnapshotSchema`):

```python
@dataclass
class BotState:
    # Portfolio
    equity: float
    day_start_equity: float

    # Positions (keyed by market_id+outcome as in risk_manager._state.open_positions)
    open_positions: dict[str, Position]

    # Recent fills (bounded deque, newest first)
    recent_fills: deque[FillRecord]

    # Strategy runtime config (three strategies)
    strategy_configs: dict[str, StrategyRuntimeConfig]

    # Kill-switch flag (HTTP handler sets this; strategy loop reads it)
    kill_switch_requested: bool

    # WS subscriber registry (per-client outbound queues)
    ws_subscribers: list[asyncio.Queue]

    # Monotonically increasing snapshot counter (replaces lastUpdatedAt clock dependency)
    last_updated_at_ms: int

    # --- v0.2.9 additions (NOT in the original D.2.1 design; landed in B.7/B.8/B.9 + 2b) ---
    # See api/state.py:107-160 for the canonical current shape.

    # Live equity + day-start (polled from RiskManager every 5s by utils.risk_state_mirror; v0.2.9 piece 2b, commit 8ff2236)
    equity: float
    day_start_equity: float

    # Bounded daily-close equity series (one EquityPoint per UTC calendar day; v0.2.9 B.7)
    equity_history: deque  # maxlen=200 (~6.5 months)

    # Bounded log + alert deques fed by utils.logs.log_to_dashboard / utils.alerts.send_alert (v0.2.9 B.8, B.9)
    recent_logs: deque    # maxlen=500
    recent_alerts: deque  # maxlen=200

    # asyncio.Lock for writes that need atomicity across multiple fields
    _write_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
```

The strategy loop acquires `_write_lock` when writing multiple fields atomically (e.g., updating equity + open_positions together on a fill). The HTTP snapshot handler reads without the lock — see D-4 for the full rationale on why lock-free reads are acceptable here.

Imports: `asyncio`, `collections.deque`, `dataclasses`, `api.schemas` (for `FillRecord`, `StrategyRuntimeConfig`).
Exposes: `BotState`.

#### `api/schemas.py`

Pydantic v2 models that mirror the dashboard's Zod schemas from `components/dashboard/schemas.ts` (per `docs/BOT_CONTRACT.md` v1.0). These are the canonical data-transfer shapes used for JSON serialisation in REST responses and WS messages.

Models to define (D.2.2 to enumerate all fields against contract Appendix B):

- `GlobalStats` — mirrors `GlobalStatsSchema`
- `StrategyCardData` — mirrors `StrategyCardDataSchema`
- `OpenPosition` — mirrors `OpenPositionSchema`
- `FillRecord` — mirrors `FillSchema`
- `RiskMetrics` — mirrors `RiskMetricsSchema`
- `StrategyConfig` — mirrors `StrategyConfigSchema`
- `BotLogEntry` — mirrors `BotLogEntrySchema`
- `AlertItem` — mirrors `AlertItemSchema`
- `EquityPoint` — mirrors `EquityPointSchema`
- `MonthlyPnl` — mirrors `MonthlyPnlSchema`
- `BacktestPoint` — **internal placeholder; does NOT mirror `BacktestPointSchema`** (intentional shape mismatch, deferred per `v029-backtest-close`; see `api/schemas.py:495-524`)
- `StrategyToggle` — mirrors `StrategyToggleSchema` structurally, but is **declared-but-unused** (no producer/consumer; not emitted on the snapshot)
- `DashboardSnapshot` — the top-level partial snapshot (all fields `Optional`)
- `StrategyConfigPatch` — request body for `POST /api/strategies/config`; mirrors `strategyPatchBodySchema`
- `WsTick` / `WsFill` / `WsAlert` / `WsStrategyUpdate` — WS message wrapper union
- `StopResponse` — `{"ok": true, "stoppedAt": int}` for emergency-stop response

All numeric fields use `float` or `int` as appropriate (no `Decimal`; the contract requires JSON numbers). `StrategyId` is a `Literal["strategy1", "strategy2", "strategy3"]`. `FillRecord.action` is `Literal["UP", "DOWN"]`. `OpenPosition.direction` is `Literal["Long", "Short"]`.

Imports: `pydantic`, `typing`.
Exposes: all model classes listed above.

#### `api/emit.py`

Provides non-blocking push functions that the strategy loop (and the REST handlers) call to broadcast events to all connected WebSocket clients. The strategy loop imports this module and calls it after fills, alerts, and strategy state changes.

Public functions (all synchronous wrappers around `asyncio.Queue.put_nowait`):

- `emit_tick(subscribers, payload: WsTick) -> None`
- `emit_fill(subscribers, payload: WsFill) -> None`
- `emit_alert(subscribers, payload: WsAlert) -> None`
- `emit_strategy_update(subscribers, payload: WsStrategyUpdate) -> None`

Each function iterates over `state.ws_subscribers`, serialises the message to JSON, and calls `queue.put_nowait(msg)`. On `asyncio.QueueFull`, TICK messages are silently dropped with a counter increment; FILL, ALERT, and STRATEGY_UPDATE log a structured warning (one-liner: `{"event": "ws_queue_full", "type": "FILL", "subscriber_count": N}`). The warning fires at most once per 60 seconds per subscriber to avoid log floods (a `dict[int, float]` keyed by `id(queue)` tracks last-warning time).

D.2.4 will wire these calls into the strategy loop's `_exec_and_record()` function and the risk manager's `on_fill()` path.

Imports: `asyncio`, `json`, `structlog`, `api.schemas`.
Exposes: `emit_tick`, `emit_fill`, `emit_alert`, `emit_strategy_update`.

---

### D-3: Runtime topology — server alongside the strategy loop

**Choice: single `asyncio.create_task(api_server.run(state, _SHUTDOWN))` added to the `tasks` list in `run_bot()` before the `asyncio.gather()` call.**

The bot's `run_bot()` function at `main.py:381-397` builds a `tasks` list and feeds it into `asyncio.gather()` at line 619 (inside the `finally` block for cancellation). The new HTTP server task is added to this list in the same pattern as `polymarket_feed`, `spot_feed`, etc.

**Port:** `8080`. Port 9091 is taken by Prometheus (`observability/metrics_exporter.py`, `config/settings.py`). The default in `BOT_CONTRACT.md` Appendix A confirms `http://localhost:8080` as the expected bot origin.

**Bind address:** Configurable via the `BOT_HTTP_API_BIND_HOST` env var (defaulting to `0.0.0.0`; the original design proposed `BOT_API_BIND_HOST`, renamed during D.2.2). In the Docker container the bot listens on all interfaces because the dashboard connects over the Docker network or the public internet. On a developer workstation, operators may override to `127.0.0.1` to prevent accidental external exposure. The existing Prometheus server binds to `127.0.0.1` (T1) but that constraint was specific to metrics; the API server must be reachable from the dashboard container, so `0.0.0.0` is the correct default.

**Shutdown:** `_SHUTDOWN` is a module-level `asyncio.Event` at `main.py:29`, set on `SIGINT`/`SIGTERM` by `_install_signal_handlers()`. `api_server.run()` receives `shutdown` as a parameter, polls `shutdown.is_set()` in its wait loop, and calls `runner.cleanup()` when signalled. The `aiohttp.web.AppRunner` teardown closes all active WebSocket connections before returning; clients will receive a normal WebSocket close frame.

**Crash behavior:** `asyncio.gather()` in `run_bot()` uses `return_exceptions=True` in the `finally` block (line 619). However, the task is added to the top-level `tasks` list, which means if the HTTP server task raises an unhandled exception it will be silently swallowed by `return_exceptions=True` in the cleanup gather. To provide visibility, `api/server.py` wraps its main loop in a `try/except Exception` that logs the exception as a structured `critical` event before re-raising. The task's exception will then propagate up through `asyncio.gather()` in the normal (non-`return_exceptions`) path of the strategy loop, causing `run_bot()` to exit. This is the correct behavior: an HTTP server crash is not recoverable in-place; the Docker container's `restart: unless-stopped` policy will restart the whole bot, which is acceptable for an unrecoverable server error.

**Sequence (how the task plugs in):** In `run_bot()`, after the `risk`, `arb`, `mom`, `mm`, `sm`, `executor` objects are created (currently around line 378), and before the `tasks = [...]` list is built, instantiate `BotState` from the initial `risk` object. Then add the API task to the `tasks` list alongside the feeds:

```python
# Pseudocode — actual implementation is D.2.2's job
from api.server import run as api_run
from api.state import BotState

bot_state = BotState.from_risk(risk, config)

tasks = [
    asyncio.create_task(_docker_alive_touch_loop(_SHUTDOWN), name="docker_alive_touch"),
    asyncio.create_task(pm_feed.run(), name="polymarket_feed"),
    asyncio.create_task(spot_feed.run(), name="spot_feed"),
    asyncio.create_task(polygon_feed.run(), name="polygon_feed"),
    asyncio.create_task(resolution_polling_loop(risk, config, _SHUTDOWN), name="resolution_poll"),
    asyncio.create_task(equity_reconciliation_loop(risk, config, _SHUTDOWN), name="equity_reconciliation"),
    asyncio.create_task(api_run(bot_state, config, _SHUTDOWN), name="api_server"),   # NEW
    asyncio.create_task(_SHUTDOWN.wait(), name="shutdown_watcher"),
]
```

The `name="api_server"` tag follows the existing naming convention and surfaces in `asyncio` task introspection / debug logs.

---

### D-3a: Auth model — REST uses Bearer, WebSocket uses signed JWT ticket

**Choice: distinct auth surfaces for REST vs WebSocket, both grounded in `config.http_api.*` secrets.** (Design originally proposed `config.api.*`; renamed during D.2.2/D.2.3 — see reader note at file head.)

The HTTP module presents two independent authentication paths, one per transport. Conflating them (e.g. accepting a Bearer header on the WebSocket upgrade) is explicitly out of scope, as is rotating either secret on the contract's v1.0 timeline (rotation is a v1.1+ concern).

**REST endpoints** — `GET /dashboard/snapshot`, `POST /api/strategies/config`, `POST /emergency-stop` (plus the 4 sim-only `/debug/*` endpoints added in v0.2.9 — see route list above). All require an `Authorization: Bearer <token>` header where `<token>` equals `config.http_api.bot_api_token` (a `SecretStr` field on `HttpApiConfig` in `config/settings.py:239`; supplied via the **`BOT_HTTP_API_BOT_API_TOKEN`** env var, NOT the `BOT_API_TOKEN` originally proposed). Handlers gate every request through `verify_bearer()` before any state read or write. Missing or mismatched tokens return HTTP 401 with no body discrimination between the two failure modes (don't leak whether the header was present-but-wrong vs absent). The token is server-to-server only — the dashboard's Next.js routes inject it (`lib/botUpstream.ts:botUpstreamHeaders()` per BOT_CONTRACT.md §3.1), never the browser.

**WebSocket endpoint** — `GET /ws` (upgrade). Auth is via `?ticket=<jwt>` query parameter (NOT `?token=` as originally proposed), where `<jwt>` is an HS256-signed JWT the dashboard mints via `GET /api/ws-ticket` using `config.http_api.ws_ticket_secret` (a separate `SecretStr` field on `HttpApiConfig`; supplied via the **`BOT_HTTP_API_WS_TICKET_SECRET`** env var). The bot verifies the JWT through `verify_ws_ticket()` before completing the upgrade handshake. The bot **shares the same secret value** as the dashboard — that's how the JWT's signature is verifiable on both sides. The bot does **not** mint tickets; it only verifies them. Invalid tickets (bad signature, wrong algorithm, expired `exp`, or already-consumed `sub`) result in WebSocket close code `4001` (per `BOT_CONTRACT.md` §3.2) without completing the handshake.

**Why two secrets, not one.** `BOT_HTTP_API_BOT_API_TOKEN` is a long-lived server-to-server credential held only by the dashboard's Next.js runtime; rotating it requires a coordinated dashboard redeploy. `BOT_HTTP_API_WS_TICKET_SECRET` signs short-lived (30-second TTL) browser-facing tickets that are themselves the auth payload; the secret never leaves the dashboard server or the bot. Treating them as one credential would either lengthen the ticket secret's exposure (it would be in the dashboard's outbound REST proxy too) or shorten the API token's effective lifetime to JWT TTL bounds. The separation matches `BOT_CONTRACT.md` §2 and the dashboard's existing `app/api/ws-ticket/route.ts` design.

**Secret loading.** Both tokens land in `config/settings.py:239` as Pydantic `SecretStr` fields on the `HttpApiConfig` nested model (`config.http_api.bot_api_token`, `config.http_api.ws_ticket_secret`). The fields default to empty `SecretStr("")` but the `_require_secrets_when_enabled` model-validator (`config/settings.py:251`) raises `ValueError` at startup if either is empty when `config.http_api.enabled=True`. A feature flag `config.http_api.enabled` (default `False`) gates whether the API task joins the gather list — sim mode runs without contract surface exposure unless the operator explicitly enables it.

---

### D-4: State-reading strategy — shared `BotState` with asyncio lock

**Choice: pattern (a) — shared mutable `BotState` with `asyncio.Lock` for writes that span multiple fields.**

Pattern (b) (read from disk/DB) would introduce staleness and coupling to log format. The contract requires snapshot latency at p95 under 5 seconds; a disk-read path is sufficient but fragile, especially since the bot does not currently write a machine-readable state snapshot file. Pattern (c) (pub-sub / materialized view) is the correct architecture for a microservice with many consumers, but the bot has at most one or two dashboard connections and the overhead of a separate materialized view is not justified.

Pattern (a) with a single `asyncio.Lock` is appropriate because:

1. The bot runs on a single-threaded asyncio event loop. Python's GIL ensures that any individual attribute assignment is atomic at the bytecode level. The lock is needed only for multi-field updates where the reader could observe a partially-updated state (e.g., equity decremented but open_positions not yet updated after a fill).

2. The snapshot HTTP handler holds the lock for a read (snapshot serialisation) without blocking the strategy loop for any meaningful duration: serialising a few hundred fields to JSON takes microseconds. A `asyncio.Lock` is non-blocking for the event loop during the `await lock.acquire()` suspension point.

3. The HTTP handler does not need to hold the lock for the entire JSON serialisation. Instead, it acquires the lock, takes a shallow copy of the mutable collections (`dict(state.open_positions)`, `list(state.recent_fills)`), releases the lock, then serialises the copy. This minimises lock hold time.

**Lock semantics:**

- `_write_lock` is an `asyncio.Lock` on `BotState`.
- **Writers** (strategy loop on fill, emergency-stop handler): `async with state._write_lock: # mutate equity, open_positions, kill_switch_requested`.
- **Reader** (snapshot handler): `async with state._write_lock: snapshot_copy = state.shallow_copy()`. Serialisation happens after releasing the lock.
- **WS emit path** (single-field writes like appending to `recent_fills`): The `deque` is bounded; `appendleft()` is atomic at the GIL level for a single-element append on CPython. `emit.py` does not acquire the lock for these single-field appends. This is an explicit assumption (see Assumptions section).

**`BotState` high-level shape** (exact fields for D.2.2):

The strategy loop already maintains all required state in `RiskManager._state` (equity, open_positions, trade_count_today) and in the strategy objects. `BotState` is a projection / cache of that state, updated by the strategy loop after each fill and at each heartbeat. The D.2.2 task will enumerate exact field mappings against `DashboardSnapshotSchema`.

---

### D-5: WebSocket emission ordering and back-pressure

**Choice: bounded `asyncio.Queue(maxsize=1000)` per connected WS client; drop-oldest on overflow for TICK; log-and-drop for FILL, ALERT, STRATEGY_UPDATE.**

**Per-client queue:** Each WebSocket connection gets its own `asyncio.Queue(maxsize=1000)`. The strategy loop calls `emit_tick(state.ws_subscribers, ...)` etc., which iterates over all registered queues and calls `put_nowait()`. There is no global shared queue; this avoids one slow client blocking another.

**Queue size (1000):** At a TICK frequency of up to ~10 applied Hz per symbol and with up to ~20 active symbols, the peak sustained rate is ~200 messages/second. A queue of 1000 gives the client roughly 5 seconds of buffering before overflow — enough for transient network hiccups without unbounded memory growth.

**Overflow policy by message type:**

| Type | On overflow | Rationale |
|------|-------------|-----------|
| `TICK` | Drop oldest + increment `dropped_tick_count` counter | TICK carries the current price; the next TICK supersedes it. Dropping intermediate ticks is the same policy the dashboard already applies client-side (`TICK_THROTTLE_MS = 100ms` in `applyWsMessage.ts`). |
| `FILL` | Log warning + drop | Fills are infrequent (per-execution-event). If a FILL is dropped, the snapshot at reconnect will reconcile. Log the drop so operators can detect pathological states. |
| `ALERT` | Log warning + drop | Alerts are low-frequency. Dropping during queue saturation is acceptable; the operator will see them on next snapshot or reconnect. |
| `STRATEGY_UPDATE` | Log warning + drop | Same as ALERT; low frequency. |

**Drop-oldest for TICK** is implemented by scanning the queue for pending TICK messages and removing the oldest one before enqueuing the new one. The clean implementation: call `put_nowait()`; if `QueueFull`, drain one item, then retry. Since `asyncio.Queue` does not expose a peek-and-remove-oldest API, the implementation will use `get_nowait()` to drain one item (discarding it) then `put_nowait()` again. This is an O(1) operation.

**Coalescing:** TICK messages are not explicitly coalesced server-side (the queue already provides natural coalescing via the drop-oldest policy above). FILL, ALERT, and STRATEGY_UPDATE are never coalesced.

**WS heartbeat:** The strategy loop does not emit heartbeat TICKs — the bot emits TICKs only when it has market data. The PING/PONG heartbeat (15s interval from client) is handled at the connection layer in `routes_ws.py`, not via the queue. PONG responses bypass the queue entirely and are sent directly to the WebSocket response object.

**Boundary for D.2.4:** D.2.4 will wire the strategy loop's `_exec_and_record()` and `risk.on_fill()` paths to call `emit.emit_fill(state.ws_subscribers, ...)`. The `emit.*` functions are the only call sites; the strategy loop itself never writes to queues directly. This boundary is enforced by import discipline: `main.py` and the strategy modules import only `emit.*` functions, not `state.ws_subscribers` directly.

---

### D-6: Test surface for D.2.2/D.2.3

**Choice: new `tests/api/` subdirectory under the existing `tests/` tree.**

The existing test tree is flat (no subdirectories currently). Adding `tests/api/` is consistent with adding a new top-level module (`api/`) and matches pytest's standard approach (mirror the source tree under `tests/`).

**Unit tests (D.2.2 and D.2.3):**

- `tests/api/test_routes_rest.py` — one test class per endpoint:
  - `GET /dashboard/snapshot`: mock `BotState`, assert 200 + valid JSON shape matching `DashboardSnapshot`.
  - `GET /dashboard/snapshot` with missing/invalid Bearer: assert 401.
  - `POST /api/strategies/config`: mock `BotState`, valid body → 200 + state mutation; invalid body → 400; missing Bearer → 401.
  - `POST /emergency-stop`: mock `BotState`, assert 200 + `{"ok": true, "stoppedAt": N}`; assert `state.kill_switch_requested == True`.
- `tests/api/test_routes_ws.py`:
  - Valid JWT → connection accepted.
  - Expired JWT → close code 4001.
  - Invalid signature → close code 4001.
  - PING → PONG response within 1s.
  - Queue overflow: enqueue >1000 TICKs, assert oldest is dropped.
- `tests/api/test_auth.py`:
  - `verify_bearer`: valid token → True; wrong token → False; missing header → False.
  - `verify_ws_ticket`: valid unexpired HS256 JWT → True; expired → False; wrong algorithm → False; reuse of sub → False (single-use).
- `tests/api/test_emit.py`:
  - `emit_fill` with N subscribers: assert all queues receive the message.
  - `emit_tick` with full queue: assert drop-oldest, no exception raised.
  - `emit_fill` with full queue: assert warning logged, no exception raised.
- `tests/api/__init__.py` — empty package marker.

**Test infrastructure:**

- Use `aiohttp.test_utils.TestClient` and `aiohttp.test_utils.TestServer` for HTTP and WebSocket handler tests. These are included in `aiohttp[dev]` extras but the test utilities are in the `aiohttp` package itself — no additional test dependency.
- Mock `BotState` using `unittest.mock.MagicMock` or a fixture factory function in `tests/api/conftest.py`.
- Mock Bearer token and WS ticket using known secrets in `conftest.py`. Never use real `.env` values in tests (CLAUDE.md hard rule 5).
- pytest-asyncio is already in `requirements.in` (`pytest-asyncio>=0.23,<1.0`); mark all async tests with `@pytest.mark.asyncio`.

**Contract tests (D.2.5):**

End-to-end contract tests (hit a live bot, validate response shapes against the dashboard's Zod schemas) are out of scope for D.2.2/D.2.3 and will be scoped in the D.2.5 integration brief. They require both the bot and the dashboard to be running simultaneously.

---

## 3. File layout (full tree)

```
api/
    __init__.py         # empty package marker
    server.py           # AppRunner setup, run() coroutine
    routes_rest.py      # GET /dashboard/snapshot, POST /api/strategies/config, POST /emergency-stop
    routes_ws.py        # WebSocket handler at /ws
    auth.py             # verify_bearer(), verify_ws_ticket()
    state.py            # BotState dataclass
    schemas.py          # Pydantic v2 models (all contract DTOs)
    emit.py             # emit_tick/fill/alert/strategy_update()
```

**New test tree:**

```
tests/api/
    __init__.py
    conftest.py         # shared fixtures (mock BotState, test tokens)
    test_auth.py
    test_emit.py
    test_routes_rest.py
    test_routes_ws.py
```

**No changes to existing files in this session.** The following files will be modified in D.2.2:

- `main.py` — add `BotState` instantiation and `api_server` task.
- `config/settings.py` — as shipped, the `HttpApiConfig` nested model at `:239` (env prefix `BOT_HTTP_API_`) exposes: `BOT_HTTP_API_ENABLED`, `BOT_HTTP_API_BIND_HOST`, `BOT_HTTP_API_PORT`, `BOT_HTTP_API_BOT_API_TOKEN` (SecretStr), `BOT_HTTP_API_WS_TICKET_SECRET` (SecretStr). The original design proposed `BOT_API_BIND_HOST` / `BOT_API_PORT` / `BOT_API_TOKEN` / `WS_TICKET_SECRET`; the rename was load-bearing (cf. D.2.3 hotfix `7c0e313`).
- `requirements.in` — add `PyJWT>=2.8.0`.

---

## 4. Runtime sequence diagram

### 4a. Snapshot request (REST)

```
Dashboard (Next.js BFF)          Bot API server          BotState        Strategy loop
        |                               |                    |                 |
        |-- GET /dashboard/snapshot --->|                    |                 |
        |   Authorization: Bearer ...   |                    |                 |
        |                               |-- verify_bearer()  |                 |
        |                               |<- True/False       |                 |
        |                               |                    |                 |
        |                               |-- acquire lock  -->|                 |
        |                               |-- shallow_copy  -->|                 |
        |                               |<- copy             |                 |
        |                               |-- release lock  -->|                 |
        |                               |                    |                 |
        |                               |-- serialize JSON   |                 |
        |<-- 200 DashboardSnapshot JSON |                    |                 |
```

### 4b. WebSocket connection and live FILL event

```
Browser                    Dashboard BFF         Bot WS server        Strategy loop
   |                            |                      |                    |
   |-- GET /api/ws-ticket ------>|                      |                    |
   |<-- {"ticket": "<jwt>"} ----|                      |                    |
   |                            |                      |                    |
   |-- WS connect ?ticket=<jwt> ---------------->|     |                    |
   |                            |           verify_ws_ticket()              |
   |                            |           <-- True                        |
   |                            |           accept + register queue         |
   |<-- connection open ----------------------------|   |                    |
   |                            |                      |                    |
   |--- {"type":"PING"} ----------------------->|      |                    |
   |<-- {"type":"PONG"} ------------------------|      |                    |
   |                            |                      |                    |
   |                            |                      |      fill occurs   |
   |                            |                      |<-- emit_fill(...)  |
   |                            |        queue.put_nowait(WsFill msg)       |
   |<-- {"type":"FILL", "payload": {...}} ---------|   |                    |
```

### 4c. Emergency stop flow

```
Browser                    Dashboard BFF         Bot REST server      BotState     Strategy loop
   |                            |                      |                  |              |
   |-- POST /api/bot/stop ------>|                      |                  |              |
   |   (cookie auth)            |                      |                  |              |
   |                            |-- POST /emergency-stop -->               |              |
   |                            |   Authorization: Bearer                  |              |
   |                            |                  verify_bearer() ------> |              |
   |                            |                  acquire lock ---------> |              |
   |                            |                  state.kill_switch_requested = True --> |
   |                            |                  release lock ---------> |              |
   |                            |                  emit_strategy_update() ->all WS clients|
   |                            |<-- 200 {"ok":true, "stoppedAt":N}        |              |
   |<-- 200 {"ok":true, ...} ---|                      |                  |              |
   |                            |                      |        (next loop iteration)    |
   |                            |                      |                  |-- kill_switch|
   |                            |                      |                  |   activated  |
```

---

## 5. Open questions

The following items must be resolved before or during D.2.2/D.2.3/D.2.4. They are listed here so the human and implementers can prioritize.

### OQ-1: PyJWT dependency — **RESOLVED**

`PyJWT>=2.8.0` is pinned in `requirements.in` and resolved to `pyjwt==2.12.1` in the hash-pinned `requirements.txt`. Used by `api/auth.py:213` for HS256 JWT verification of WS tickets. `cryptography` is already a transitive dependency (via `eth-account`), so PyJWT's optional `cryptography` extras are satisfied. No further action.

### OQ-2: `HttpApiConfig` env vars in `.env.example` — **PARTIALLY RESOLVED, real gap remains**

The shipped env vars (per `config/settings.py:239` `HttpApiConfig` with `env_prefix="BOT_HTTP_API_"`) are:

- **`BOT_HTTP_API_ENABLED`** (bool, default `False`) — feature flag; the API task only joins the gather list when `true`.
- **`BOT_HTTP_API_BIND_HOST`** (str, default `0.0.0.0`) — bind address for the aiohttp server.
- **`BOT_HTTP_API_PORT`** (int, default `8080`) — listen port.
- **`BOT_HTTP_API_BOT_API_TOKEN`** (SecretStr) — REST Bearer token the dashboard's `Authorization: Bearer` header carries. Fail-closed: `_require_secrets_when_enabled` model-validator at `config/settings.py:251` raises `ValueError` at startup if empty when `BOT_HTTP_API_ENABLED=true`. (The original design proposed `BOT_API_TOKEN`; renamed during D.2.2/D.2.3.)
- **`BOT_HTTP_API_WS_TICKET_SECRET`** (SecretStr) — HS256 key shared with the dashboard's `WS_TICKET_SECRET`. Must be identical on both sides. Same fail-closed validator. (The original design proposed bare `WS_TICKET_SECRET`; renamed during D.2.2/D.2.3.)

**Real gap (separate from this doc; flagged 2026-05-27 audit):** none of the five `BOT_HTTP_API_*` env vars currently appear in `.env.example` (verified empty `grep`). Operators turning on the HTTP API must consult `README.md:15`, `config/settings.py:239`, or the dashboard's own README — there's no template entry. Tracked as a separate `.env.example` follow-up; not part of this D.2.1 design correction.

### OQ-3: Exact `BotState` field mappings (D.2.2 to enumerate)

D.2.2's first task is to map every field in `DashboardSnapshotSchema` to a source in `RiskManager._state`, the strategy objects, or the executor. Some fields (e.g., `backtest`, `monthlyPnl`, `equity` chart history) may not have direct runtime sources and may require either (a) maintaining a rolling history in `BotState`, (b) serving stub/empty arrays until telemetry is wired, or (c) deferring to the next phase. D.2.2 must make this explicit and document any stubs.

**Resolution (v0.2.9):** `equity` chart history now has a runtime source — `utils.equity_curve.equity_snapshot_loop` (B.7) writes one `EquityPoint` per UTC calendar day to a bounded `BotState.equity_history` (`deque(maxlen=200)`); see `.swarm-decisions.md` entry `v029-b7-equity-wiring`. `risk` is now produced by `api/routes_rest.py:_build_riskmetrics` (3e canonical 6-field RiskMetrics). `logs` and `alerts` are now populated by `utils.logs.log_to_dashboard` (B.8) and `utils.alerts.send_alert` (B.9) respectively. `monthlyPnl` is now LIVE (computed at snapshot-build via `compute_monthly_pnl`, `api/routes_rest.py:340`); `backtest` is a **permanent intentional stub** — reshaping to per-strategy is a feature-build, not a wiring task (`api/routes_rest.py:341` ships `[]`; closed per `v029-backtest-close`).

### OQ-4: Strategy ID mapping (strategy1/strategy2/strategy3 vs internal names)

The contract uses stable IDs `strategy1`, `strategy2`, `strategy3`. The bot's internal strategy names are `intra_arbitrage`, `momentum_latency`, `market_maker` (and optionally `smart_money_copy`). The mapping must be:

| Contract ID | Internal name |
|-------------|---------------|
| `strategy1` | `intra_arbitrage` |
| `strategy2` | `momentum_latency` |
| `strategy3` | `market_maker` |

`smart_money_copy` has no contract ID in v1.0; it is off by default and not exposed to the dashboard. This mapping should be encoded as a constant in `api/schemas.py` and used consistently.

### OQ-5: `StrategyRuntimeConfig` vs `config/settings.py` — mutation scope

`POST /api/strategies/config` mutates `strategy_configs` in `BotState`. This must propagate back to the actual strategy objects (`arb`, `mom`, `mm` in `run_bot()`). The cleanest approach is to have the strategy objects read from `BotState.strategy_configs` on each loop iteration rather than from `config` (which is an immutable `lru_cache` singleton). D.2.2 must decide whether to (a) make the strategy objects reference `BotState` configs at runtime, (b) add a `update_config(StrategyRuntimeConfig)` method to each strategy, or (c) store the mutable config in a separate mutable object that both the strategy and the API handler reference. This is a design decision for D.2.2 to make and document.

### OQ-6: Prometheus metrics for the API server

The existing Prometheus exporter (`observability/metrics_exporter.py`) exposes bot trading metrics on port 9091. Exposing API server metrics (request count, WS connection count, queue depth) is not required by the contract but follows the bot's observability pattern. Recommendation: add a small set of gauges/counters to the metrics exporter for WS connection count and REST request rate. Out of scope for D.2.2; flag for D.2.4 or a dedicated observability pass.

### OQ-7: Sequence number (Option A reconciliation)

`docs/BOT_CONTRACT.md` v1.0 "Open questions" lists adding a monotonic `sequenceNumber` to TICK payloads and snapshot responses as a future enhancement (Option A). This spec does not implement sequence numbers — they are v1.1 or v2.0. D.2.2 should leave a TODO comment at the snapshot serialisation point referencing this open question so future implementers can find it.

---

## 6. Assumptions

The following were taken as given based on reading `docs/BOT_CONTRACT.md` v1.0, `main.py`, `risk_manager.py`, `data_feeds/spot_feed.py`, `requirements.in`, and `requirements.txt`. If any assumption is wrong, the spec section that depends on it is noted.

**A1. `aiohttp==3.10.10` includes `aiohttp.web` (server-side).** True — `aiohttp` ships both client and server components in the same package. The server-side `aiohttp.web` module has been available since aiohttp 1.x. Confirmed by: `requirements.in` line 35 `aiohttp==3.10.10`. No additional package needed. (Affects D-1.)

**A2. `asyncio.Lock` is safe for use across the strategy loop and the HTTP handler on the same event loop.** True — all tasks run on a single-threaded asyncio event loop. `asyncio.Lock` is designed for this pattern. Lock contention between the strategy loop (which acquires for ~microseconds per fill) and the snapshot handler (which acquires for ~microseconds to copy state) is negligible. (Affects D-4.)

**A3. `deque.appendleft()` is atomic at the CPython GIL level for a single append.** True — CPython's GIL ensures that individual `deque` operations are not interrupted mid-operation. Since all tasks run on one event loop thread, there is no concurrent modification risk anyway. (Affects D-4, `api/state.py` emit path.)

**A4. The strategy loop's `_SHUTDOWN` event is the canonical shutdown signal for all tasks.** True — `main.py:29` defines `_SHUTDOWN = asyncio.Event()` and `_install_signal_handlers()` sets it on SIGINT/SIGTERM. All existing background tasks poll this event. The API server task follows the same pattern. (Affects D-3.)

**A5. Port 8080 is available in the Docker container.** The `docker-compose.yml` currently exposes port 9091 for Prometheus. Port 8080 is not occupied by any existing service. A new port mapping `8080:8080` must be added to `docker-compose.yml` and documented in `deploy/DEPLOY_DO.md` before production deployment. This is a D.2.2 deployment task. (Affects D-3.)

**A6. `PyJWT` (not `python-jose`) is the correct JWT library.** PyJWT is the most widely maintained HS256 JWT library for Python; it is a direct dependency of many packages in the ecosystem. `python-jose` is an alternative but has had security advisories and is less actively maintained as of 2025. PyJWT's API for HS256 verification is: `jwt.decode(token, secret, algorithms=["HS256"])` — simple and unambiguous. The `algorithms=` parameter must be an explicit allowlist to prevent algorithm-confusion attacks (never pass `algorithms=None` or omit it). (Affects `api/auth.py`.)

**A7. The dashboard connects to the bot over the Docker network or public internet, not loopback only.** The `BOT_ORIGIN` default in `docs/BOT_CONTRACT.md` Appendix A is `http://localhost:8080`, but in production the dashboard (on Digital Ocean) connects to the bot's actual host. The API server therefore binds to `0.0.0.0` by default, not `127.0.0.1`. This is different from the Prometheus server (which binds to `127.0.0.1` per T1). The distinction is intentional: the API server is the bot's public interface; Prometheus is a host-local scrape target. (Affects D-3.)

**A8. `STRATEGY_UPDATE` emitted after `POST /api/strategies/config` is the correct mechanism for notifying other dashboard sessions.** Per `docs/BOT_CONTRACT.md` "Side effect" note under `POST /api/strategies/config`: "After applying config, the bot should emit STRATEGY_UPDATE on WebSocket so other connected dashboards converge." The emergency-stop handler also emits `STRATEGY_UPDATE` per the contract's Semantics section. (Affects `api/routes_rest.py`.)

**A9. The single-use `sub` JWT tracking for WebSocket tickets can be in-process memory (not Redis).** The `docs/BOT_CONTRACT.md` notes the dashboard's in-memory ticket map has a TODO for Redis/KV in multi-instance deployments. The bot is a single-process deployment (one Docker container). In-process tracking of consumed `sub` values in `api/auth.py` is sufficient. The 30-second TTL is the minimum mitigation even if the tracking is unavailable. (Affects `api/auth.py`.)

**A10. `StrategyCardData.pipelineStep` maps to the strategy's priority index (1, 2, 3, 4).** The contract defines `pipelineStep` as a field in `StrategyCardDataSchema` but does not specify its semantics. The assumption is that it maps to the bot's strategy priority order: `intra_arbitrage=1`, `momentum_latency=2`, `market_maker=3`, `smart_money_copy=4`. D.2.2 should confirm this with the human before implementing the snapshot serialiser. (Affects `api/state.py`, `api/schemas.py`.)

---

## 7. What is explicitly out of scope for D.2.1

- Actual Python code for any of the above modules (that is D.2.2/D.2.3/D.2.4).
- Changes to `requirements.in` or `requirements.txt` (D.2.2 adds PyJWT after human approval).
- Changes to `main.py`, `config/settings.py`, `risk_manager.py`, or any existing module.
- The D.2.5 integration test session.
- Sequence number implementation (OQ-7 — v1.1 or v2.0 feature).
- Prometheus metrics for the API server (OQ-6 — deferred).

---

*End of D.2.1 architecture spec.*
*Drafted by Team B architect, swarm session 2026-05-12.*
*Human review and commit required before D.2.2 begins.*
