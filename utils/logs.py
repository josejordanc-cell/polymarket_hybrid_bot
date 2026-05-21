"""
utils/logs.py
v0.2.9 B.8 wiring — dashboard log-feed broadcast helper.

Companion to ``utils/alerts.py`` (B.9 wiring). Where ``send_alert``
populates ``BotState.recent_alerts`` and broadcasts ALERT WS frames,
``log_to_dashboard`` populates ``BotState.recent_logs`` for the
snapshot's ``logs[]`` slice. **No WS broadcast** — the Zod
``WsMessageSchema`` (components/dashboard/schemas.ts:226) does NOT
have a ``LOG`` discriminated-union variant today; if LOG frames are
ever needed, that's a Team A schema-extension sub-phase + a new
``WsLog`` envelope here.

Module-level singleton lifecycle mirrors ``utils/alerts.set_emitter``
and ``api/state.set_state``: registered once at HTTP API server
startup; tests reset via ``set_state(None)``.

Locked policy (3f Option C lossy map applied INSIDE
``log_to_dashboard``):

    debug    → DROP   (entry excluded from BotState.recent_logs)
    info     → "INFO"
    warning  → "WARN"
    error    → "ERROR"
    critical → "ERROR"   (lossy collapse to highest wire tier)

Bot NEVER emits ``"EXEC"`` (vestigial slot per BOT_CONTRACT.md;
revisited if B.1 fill runtime gets its own dashboard slice).
``log_to_dashboard`` is the only producer of wire-shape BotLogEntry
instances on the bot side today, so the policy is enforced at one
choke-point.

The 4-field BotLogEntry has no ``source`` field (3f canonical:
``id, time, level, message``). ``log_to_dashboard``'s ``source``
parameter is folded into ``message`` as ``f"[{source}] {message}"``
— mirrors how ``send_alert`` prepares the Telegram/Discord text.
"""
from __future__ import annotations

import time as _time_mod
import uuid
from typing import Literal, Optional

import structlog

from api.schemas import BotLogEntry
from api.state import get_state

logger = structlog.get_logger(__name__)


# v0.2.9 3f Option C (Jose 2026-05-21): internal→wire log-level map.
# Python's standard 5-tier set (debug/info/warning/error/critical)
# collapses lossy to the 4-tier dashboard enum (INFO/WARN/ERROR/EXEC).
# Bot NEVER emits EXEC.
_LOG_LEVEL_MAP: dict[str, Optional[str]] = {
    "debug": None,        # drop debug entries from the log feed
    "info": "INFO",
    "warning": "WARN",
    "error": "ERROR",
    "critical": "ERROR",  # lossy collapse to highest wire tier
}


# Internal-tier ``Literal`` for type-checking the public API. Matches
# Python's stdlib logging vocabulary.
InternalLogLevel = Literal["debug", "info", "warning", "error", "critical"]


def _map_log_level(internal: str) -> Optional[str]:
    """Map a Python-logging level (lowercase) to the wire BotLogEntry
    ``level`` enum literal, or ``None`` to drop the entry.

    Raises ``ValueError`` on unknown internal levels — silent corruption
    of the wire enum would be harder to debug than a surfaced exception
    at the boundary. Mirrors the defensive policy used by
    ``api/routes_rest.py:_map_alert_severity`` /
    ``_collapse_position_id``.
    """
    try:
        return _LOG_LEVEL_MAP[internal]
    except KeyError as exc:
        raise ValueError(
            f"Unknown internal log level {internal!r}; "
            f"expected one of {sorted(_LOG_LEVEL_MAP.keys())!r}"
        ) from exc


async def log_to_dashboard(
    message: str,
    level: InternalLogLevel = "info",
    source: str = "bot",
) -> None:
    """Append a wire-shape BotLogEntry to ``BotState.recent_logs`` so
    the snapshot's ``logs[]`` slice surfaces the entry to the dashboard.

    v0.2.9 B.8 wiring contract:
      - ``level`` is the INTERNAL 5-tier vocabulary
        (debug/info/warning/error/critical); ``_map_log_level``
        collapses to wire 4-tier (INFO/WARN/ERROR) per 3f Option C.
      - ``debug`` entries are silently dropped (returns without
        writing) — verbose-only tier, never reaches the dashboard.
      - ``source`` is folded into ``message`` as
        ``f"[{source}] {message}"`` because the 4-field BotLogEntry
        canonical shape has no separate ``source`` field. Mirrors
        ``send_alert``'s Telegram/Discord text composition.
      - If no ``BotState`` is registered (``get_state() is None``),
        the call silently no-ops — matches the headless/test-environment
        no-op of ``send_alert``.

    Fire-and-forget: never raises. All errors are logged and swallowed.

    No WS broadcast: the Zod ``WsMessageSchema`` does NOT have a
    ``LOG`` variant today (verified against
    components/dashboard/schemas.ts:226). Snapshot is the only
    transport. If/when LOG WS frames are added on the dashboard side,
    this helper grows a broadcast path symmetric with ``send_alert``.
    """
    try:
        wire_level = _map_log_level(level)
        if wire_level is None:
            # ``debug`` tier — silently dropped per Option C policy.
            # Not an error condition; just below the wire's noise floor.
            return

        state = get_state()
        if state is None:
            # Headless / unit-test environment without a registered
            # BotState singleton — no destination for the log entry.
            return

        entry = BotLogEntry(
            id=uuid.uuid4().hex,
            time=_time_mod.strftime("%H:%M:%S", _time_mod.localtime()),
            level=wire_level,
            message=f"[{source}] {message}",
        )

        async with state.write_lock:
            state.recent_logs.append(entry)
    except Exception as exc:
        # Defensive: log-feed wiring must NOT kill the caller. Any
        # exception (Pydantic validation, lock acquisition, etc.) is
        # logged through the bot's own structlog (NOT recursively via
        # log_to_dashboard) and swallowed.
        logger.warning(
            "dashboard_log_append_failed",
            error=str(exc),
            level=level,
            source=source,
        )
