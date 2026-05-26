"""
api/server.py — Entry-point coroutine for the bot HTTP/WebSocket module.

Invoked from main.py:run_bot() via asyncio.create_task(api_run(...)) when
config.http_api.enabled is True. Runs an aiohttp Application that serves
the three REST routes registered by api.routes_rest.setup_routes plus
the GET /ws WebSocket route registered by api.routes_ws.ws_handler.

The Emitter is owned by ``run()`` and lives for the lifetime of the
server task. It is attached to the Application via the typed
``EMITTER_KEY`` declared in ``api/routes_ws`` so the handler can read
it on every connection. ``CONFIG_KEY`` carries the BotConfig that
``verify_ws_ticket`` needs to read the HS256 shared secret.

D.2.3 sets up the broadcaster infrastructure; D.2.4 will wire
``emitter.broadcast(...)`` into the strategy loop's fill / alert /
strategy-update paths.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog
from aiohttp import web

from api.emit import Emitter
from api.routes_rest import setup_routes
from api.routes_ws import CONFIG_KEY, EMITTER_KEY, ws_handler
from api.state import BotState

logger = structlog.get_logger(__name__)


# Polling interval for the shutdown-event check. aiohttp's AppRunner does
# not expose a native event-driven shutdown hook at this layer, so we poll.
# 0.5s is low overhead and gives sub-second shutdown latency in practice.
_SHUTDOWN_POLL_INTERVAL_S: float = 0.5


async def run(
    state: BotState,
    config: Any,
    shutdown: asyncio.Event,
    *,
    risk: Any = None,
) -> None:
    """Start the aiohttp HTTP server and run until ``shutdown`` is set.

    Behavior:
      - Builds an ``aiohttp.web.Application``, delegates route registration
        to ``api.routes_rest.setup_routes(app, state, config)``.
      - Reads ``config.api.bind_host`` and ``config.api.port`` via
        ``getattr`` with safe defaults (``"0.0.0.0"`` / ``8080``). Step 6
        wires the real nested ``HttpApiConfig`` model into BotConfig; until
        then the defaults let this module be imported and exercised stand-
        alone.
      - Starts an ``AppRunner`` + ``TCPSite`` and polls ``shutdown`` at
        ``_SHUTDOWN_POLL_INTERVAL_S`` intervals.
      - Logs ``api_server_started`` / ``api_server_stopped`` info events
        and ``api_server_fatal_error`` critical-with-exc_info on unexpected
        failure (D.2.1 §D-3 crash-behavior paragraph). Re-raises so the
        parent task gather sees the failure.
      - Cleans up the runner unconditionally in ``finally``.

    The signature is stable across D.2.2 / D.2.3 / D.2.4 — D.2.3 will add a
    second `setup_routes` call for WS routes, without changing this entry.
    """
    app = web.Application()
    setup_routes(app, state, config)

    # Emitter + WS route are owned by run() for the lifetime of the
    # server task. The emitter is attached to the Application via
    # typed AppKeys so api.routes_ws.ws_handler can read it on each
    # upgrade. D.2.4 will plumb a reference into the strategy loop
    # (likely through BotState or a top-level wiring) to make
    # broadcast() calls from the fill / alert / strategy-update paths.
    emitter = Emitter()
    app[EMITTER_KEY] = emitter

    # D.2.4 steps 1-2: make the Emitter reachable from
    # utils/alerts.send_alert and utils/fills.broadcast_fill via
    # module-level singletons. See d24-emit-site-audit.md §9.1, §9.2.
    #
    # NOTE: utils/strategy_updates also has a set_emitter helper,
    # but no correct emit site exists in v0.2.8 (the d1d2276 wire-up
    # was contract-nonconformant and reverted). v0.2.9 will register
    # it here once the telemetry-pipeline emit site lands.
    from utils.alerts import set_emitter as _set_alerts_emitter
    from utils.fills import set_emitter as _set_fills_emitter
    _set_alerts_emitter(emitter)
    _set_fills_emitter(emitter)

    # v0.2.9 B.9 wiring: register the BotState singleton so
    # utils/alerts._broadcast_dashboard can also append to
    # state.recent_alerts (snapshot history of alerts). Idempotent;
    # mirrors the set_emitter lifecycle above. See
    # .swarm-decisions.md v029-b9-alerts-wiring.
    from api.state import set_state as _set_bot_state
    _set_bot_state(state)

    # v0.2.9 B.7 wiring: start the equity-snapshot background loop.
    # Bootstraps today's EquityPoint immediately on first iteration so
    # the snapshot's equity[] slice is non-empty from boot+1s; then
    # polls hourly for UTC-day rollovers to append the next day's
    # point. Lifecycle: created here, cancelled in the finally block
    # below so shutdown is clean (no orphaned tasks). See
    # .swarm-decisions.md v029-b7-equity-wiring.
    from utils.equity_curve import equity_snapshot_loop
    equity_task: asyncio.Task = asyncio.create_task(
        equity_snapshot_loop(state),
        name="equity_snapshot_loop",
    )

    # Profitability epic piece (2b) wiring: start the RiskManager →
    # BotState polling mirror. Atomically writes risk.equity /
    # risk.open_positions / risk.daily_pnl_snapshot().start_equity_usd
    # into BotState every 5s so /dashboard/snapshot returns live values
    # rather than the frozen startup snapshot from BotState.from_risk.
    # Reads use public accessors only — no risk_manager.py edits (FENCED).
    # Lifecycle mirrors equity_task exactly. Gated on risk being passed:
    # tests that call run() without risk (kwarg-only, default None) skip
    # the mirror task entirely, preserving the pre-(2b) test signature.
    # See utils/risk_state_mirror.py and .swarm-decisions.md
    # b1-fill-runtime-update for the parallel (2a) deferral context.
    risk_mirror_task: "asyncio.Task | None"
    if risk is not None:
        from utils.risk_state_mirror import mirror_risk_to_botstate
        risk_mirror_task = asyncio.create_task(
            mirror_risk_to_botstate(risk, state),
            name="risk_state_mirror",
        )
    else:
        risk_mirror_task = None

    app[CONFIG_KEY] = config
    app.router.add_get("/ws", ws_handler)

    api_cfg = getattr(config, "http_api", None)
    port: int = getattr(api_cfg, "port", 8080)
    bind_host: str = getattr(api_cfg, "bind_host", "0.0.0.0")

    runner = web.AppRunner(app)
    try:
        await runner.setup()
        site = web.TCPSite(runner, bind_host, port)
        await site.start()
        logger.info("api_server_started", host=bind_host, port=port)

        while not shutdown.is_set():
            await asyncio.sleep(_SHUTDOWN_POLL_INTERVAL_S)

    except Exception:
        logger.critical("api_server_fatal_error", exc_info=True)
        raise
    finally:
        # v0.2.9 B.7: cancel + await the equity-snapshot loop BEFORE
        # tearing down the runner so the loop sees CancelledError
        # cleanly. The loop propagates the cancellation and logs an
        # info event on exit. The await here swallows CancelledError
        # (expected) but re-raises any other exception (unexpected).
        equity_task.cancel()
        try:
            await equity_task
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            logger.warning(
                "equity_snapshot_loop_shutdown_error", exc_info=True
            )

        # (2b) mirror shutdown: identical cancel-and-await pattern.
        # Only present when risk was passed at start; tests that omit
        # risk skip both creation and teardown.
        if risk_mirror_task is not None:
            risk_mirror_task.cancel()
            try:
                await risk_mirror_task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                logger.warning(
                    "risk_state_mirror_shutdown_error", exc_info=True
                )

        await runner.cleanup()
        logger.info("api_server_stopped")
