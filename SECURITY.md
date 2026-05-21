# Security policy

## Supported versions

Only the most recent minor release receives security fixes. Older versions
are out of scope.

| Version | Supported |
|---|---|
| 0.2.x | ✅ |
| 0.1.x and earlier | ❌ |

## Reporting a vulnerability

**Do not file public issues for security bugs.**

Report privately via one of the following channels (in order of preference):

1. GitHub Private Vulnerability Reporting — open the repository's
   "Security" tab and use "Report a vulnerability". This is the preferred
   channel because it produces an audit trail visible to maintainers without
   exposing the report publicly.
2. Email: x.jjtrader@gmail.com.

Include in the report:

- A description of the vulnerability and the affected component (data feed,
  execution, risk manager, etc.).
- Reproduction steps or proof-of-concept.
- The version / commit hash you tested against.
- Your assessment of severity and exploitability.
- Whether you have already disclosed to anyone else.

You will receive an acknowledgement within 72 hours and a substantive
response within 7 days. Coordinated disclosure target: 90 days from report,
shorter if active exploitation is observed.

## Out of scope

- Bugs that require already-compromised host access (root on the droplet,
  read access to `.env`) — those are operational issues, not vulnerabilities.
- Findings against deprecated versions.
- Polymarket / Polygon / upstream library bugs — please report those upstream.
- Theoretical issues with no demonstrated path to harm.

## In scope

- Anything that lets an unauthenticated party place, cancel, or modify orders.
- Anything that lets an unauthenticated party read trading state, fills, PnL,
  or wallet identifiers.
- Anything that lets an authenticated dashboard user escalate beyond their
  documented scope (once the dashboard ships).
- Secret-leak vectors in logs, error messages, metrics endpoints, telegram
  alerts, or discord webhooks.
- Supply-chain issues (typosquats, hijacked dependencies, compromised
  base-image digests).
- Container-escape vectors specific to this image.

## Debug-injection endpoints (v0.2.9)

Four `POST /debug/*` endpoints were added during the v0.2.9 wave for
runtime-smoke purposes:

- `POST /debug/inject-position` — writes a synthetic Position into
  `BotState.open_positions` AND appends a corresponding `FillRecord`
  to `BotState.recent_fills`.
- `POST /debug/clear-positions` — empties both deques.
- `POST /debug/inject-alert` — fires `utils.alerts.send_alert`
  internally (state-write + WS broadcast).
- `POST /debug/inject-log` — fires `utils.logs.log_to_dashboard`
  internally (state-write only; no LOG WS variant on the dashboard).

**Intended for sim-mode runtime smoke only. Do NOT expose to
production traffic.**

Each handler enforces five security properties (defense-in-depth):

1. **Sim-mode 404 BEFORE auth.** Handler returns `404 Not Found` when
   `config.runtime.mode != "sim"`. The 404 fires before the Bearer
   check — non-sim deployments cannot distinguish "endpoint exists
   but is disabled" from "endpoint does not exist." The route is
   registered unconditionally; the gate is enforced inside the
   handler (`_verify_debug_mode` walks `config.runtime.mode`).
2. **Bearer auth required.** Same `verify_bearer` surface as
   `/dashboard/snapshot`; missing or wrong token → 401 (in sim mode
   only; live mode 404s above).
3. **Loopback bind expected.** `HttpApiConfig.bind_host` default
   (`config/settings.py:245`) is `"0.0.0.0"` (frozen surface);
   production `.env` overrides with `BOT_HTTP_API_BIND_HOST=127.0.0.1`.
   The mode-gate + Bearer auth provide defense-in-depth if a deploy
   ever lands without the loopback override.
4. **Atomic write under `state.write_lock`.** All state mutations
   happen inside `async with state.write_lock` so snapshot reads stay
   consistent.
5. **INFO log audit trail.** Every successful injection logs
   `debug_position_injected` / `debug_alert_injected` /
   `debug_log_injected` / `debug_positions_cleared` with the full
   request payload + resulting id. Pydantic input validation
   (`InjectPositionRequest`, `InjectAlertPayload`, `InjectLogPayload`)
   rejects malformed bodies with 400 before any state mutation.

Special case: `POST /debug/inject-log` with `level="debug"` returns
`204 No Content` (intentional drop per 3f Option C lossy log-level
map; debug-tier entries do not surface to the dashboard).

See `.swarm-decisions.md` entries `v029-3h`, `v029-3h-fix`,
`v029-b9-fix`, `v029-b8-logs-wiring` for per-endpoint design
rationale.
