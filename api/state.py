"""
api/state.py
BotState — the single shared-state object passed by reference to both the
strategy loop (writer) and the API server task (reader).

Design references:
  D.2.1 §3 (api/state.py file description)
  D.2.1 §4 (BotState high-level shape)
  D.2.1 §D-4 (lock semantics and shallow_copy rationale)
  D.2.1 §OQ-3 (stub fields — present but empty until D.2.4 wires them)
  D.2.1 §OQ-4 (strategy ID mapping — encoded in api/schemas.py)

Key invariants:
  - BotState is instantiated ONCE in run_bot(), before the task graph.
  - The strategy loop acquires write_lock (public accessor) for multi-field
    atomic writes.
  - The snapshot handler acquires write_lock, calls shallow_copy(), then
    releases the lock BEFORE serialising. (Minimises lock hold time.)
  - ws_subscribers is NOT copied by shallow_copy() — it is the live list.
  - recent_fills is a deque(maxlen=200); recent_logs maxlen=500;
    recent_alerts maxlen=200; equity_history maxlen=200 (v0.2.9 B.7).
    Bounded in-process storage.
  - from_risk() uses getattr with safe fallbacks so MagicMock passes (§6).
  - No imports from main.py, risk_manager.py, or strategies/ in this file.
"""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from api.schemas import (
    FillRecord,
    StrategyId,
    StrategyRuntimeConfig,
)

# ---------------------------------------------------------------------------
# Default per-strategy runtime configs (three strategies, enabled=True)
# ---------------------------------------------------------------------------

_DEFAULT_STRATEGY_CONFIGS: dict[str, StrategyRuntimeConfig] = {
    "strategy1": StrategyRuntimeConfig(strategyId="strategy1", enabled=True),
    "strategy2": StrategyRuntimeConfig(strategyId="strategy2", enabled=True),
    "strategy3": StrategyRuntimeConfig(strategyId="strategy3", enabled=True),
}


# ---------------------------------------------------------------------------
# Module-level BotState singleton (v0.2.9 B.9 wiring)
# ---------------------------------------------------------------------------
#
# Registered once at HTTP API server startup via ``set_state``. Modules
# that need to write into ``BotState.recent_alerts`` / similar collection
# fields (utils/alerts.py for now; utils/fills.py and others later) call
# ``get_state()`` and silently no-op if no state is registered. Mirrors
# the precise pattern of ``utils/alerts.set_emitter`` /
# ``utils/fills.set_emitter`` — same lifecycle, same idempotence, same
# test-side ``set_state(None)`` reset.
#
# Why this lives here (not in the modules that consume it): keeping the
# accessor next to ``BotState`` makes the state-singleton lifecycle
# discoverable from one place, avoids per-module state-pointer drift,
# and matches the precedent established for the Emitter singleton.
# ---------------------------------------------------------------------------

_state: "BotState | None" = None


def set_state(state: "BotState | None") -> None:
    """Register (or clear) the process-wide BotState singleton.

    Called from ``api/server.py`` once the HTTP API server initialises
    the canonical BotState. Idempotent — safe to call multiple times;
    the most recent value wins. Passing ``None`` clears the
    registration (used by tests to isolate state between cases)."""
    global _state
    _state = state


def get_state() -> "BotState | None":
    """Return the currently registered BotState, or None if unset."""
    return _state


# ---------------------------------------------------------------------------
# BotState
# ---------------------------------------------------------------------------


@dataclass
class BotState:
    """
    Shared state between the strategy loop and the HTTP/WebSocket API server.

    Writers: strategy loop (fills, equity, positions, kill-switch).
    Readers: HTTP snapshot handler, WS broadcast path.

    Fields marked OQ-3 are stubs: present as empty collections so the
    D.2.3 serialiser can emit empty arrays without schema changes.
    """

    # ------------------------------------------------------------------
    # Portfolio
    # ------------------------------------------------------------------
    equity: float
    day_start_equity: float

    # ------------------------------------------------------------------
    # Positions  (keyed by "<market_id>:<outcome>", mirrors
    #             risk_manager._state.open_positions structure)
    # ------------------------------------------------------------------
    open_positions: dict[str, Any]  # values are utils.types.Position instances

    # ------------------------------------------------------------------
    # Recent fills  (bounded deque, newest at left)
    # ------------------------------------------------------------------
    recent_fills: deque  # deque[FillRecord], maxlen=200

    # ------------------------------------------------------------------
    # Per-strategy mutable runtime config
    # (keyed by contract strategy ID: "strategy1" / "strategy2" / "strategy3")
    # ------------------------------------------------------------------
    strategy_configs: dict[str, StrategyRuntimeConfig]

    # ------------------------------------------------------------------
    # WS subscriber registry — per-client outbound queues.
    # NOT copied by shallow_copy(). The live list is always shared.
    # ------------------------------------------------------------------
    ws_subscribers: list  # list[asyncio.Queue]

    # ------------------------------------------------------------------
    # Kill-switch flag
    # HTTP handler sets this; strategy loop reads it on next iteration.
    # ------------------------------------------------------------------
    kill_switch_requested: bool = False

    # ------------------------------------------------------------------
    # Monotonically increasing snapshot counter (epoch ms at last update).
    # Replaces clock dependency in callers; set by strategy loop on each
    # fill or heartbeat.
    # ------------------------------------------------------------------
    last_updated_at_ms: int = 0

    # ------------------------------------------------------------------
    # OQ-3 stub fields — empty until D.2.4 wires telemetry
    # ------------------------------------------------------------------
    risk_metrics: Any = None                              # OQ-3
    equity_history: deque = field(                       # B.7, maxlen=200
        default_factory=lambda: deque(maxlen=200)
    )
    monthly_pnl: list = field(default_factory=list)      # OQ-3
    backtest: list = field(default_factory=list)          # OQ-3
    recent_logs: deque = field(                           # OQ-3, maxlen=500
        default_factory=lambda: deque(maxlen=500)
    )
    recent_alerts: deque = field(                         # OQ-3, maxlen=200
        default_factory=lambda: deque(maxlen=200)
    )

    # ------------------------------------------------------------------
    # asyncio.Lock for multi-field atomic writes.
    # repr=False keeps dataclass __repr__ readable; not included in
    # shallow_copy() (a new lock is created for each copy).
    # ------------------------------------------------------------------
    _write_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock, repr=False, compare=False
    )

    @property
    def write_lock(self) -> asyncio.Lock:
        """Public accessor for the multi-field atomic-write lock.

        Use as ``async with state.write_lock: ...`` from API handlers.
        The underscore-prefixed attribute remains accessible for internal
        use within this module but should not be touched from sibling
        packages.
        """
        return self._write_lock

    # ------------------------------------------------------------------
    # Constructor helpers
    # ------------------------------------------------------------------

    @classmethod
    def create(
        cls,
        equity: float,
        day_start_equity: float,
        open_positions: dict[str, Any] | None = None,
        strategy_configs: dict[str, StrategyRuntimeConfig] | None = None,
        recent_fills: deque | None = None,
    ) -> "BotState":
        """
        Convenience factory for test fixtures and direct construction.
        Uses safe defaults so callers don't have to supply every field.
        """
        return cls(
            equity=equity,
            day_start_equity=day_start_equity,
            open_positions=open_positions if open_positions is not None else {},
            recent_fills=(
                recent_fills
                if recent_fills is not None
                else deque(maxlen=200)
            ),
            strategy_configs=(
                strategy_configs
                if strategy_configs is not None
                else {k: v.model_copy() for k, v in _DEFAULT_STRATEGY_CONFIGS.items()}
            ),
            ws_subscribers=[],
        )

    @classmethod
    def from_risk(cls, risk: Any, config: Any) -> "BotState":
        """
        Construct a BotState from a RiskManager instance and a BotConfig.

        Uses getattr() with safe fallbacks throughout so that a
        unittest.mock.MagicMock (or a partially-initialised RiskManager)
        does not raise AttributeError.  This satisfies the requirement in
        SESSION-BRIEF §6 that MagicMock passes from_risk() cleanly.

        Reads ``config.strategy.momentum_enabled`` and
        ``config.strategy.market_maker_enabled`` to propagate the
        environment-driven enable/disable state into the returned
        ``strategy_configs``.  Uses a ``getattr`` chain so that a bare
        ``MagicMock`` (without a fully-specified ``strategy`` attribute)
        passes cleanly — the fallback is ``True``, preserving the
        previous behaviour for tests that do not build a real BotConfig.
        Real ``BotConfig`` instances always have ``cfg.strategy``
        populated via the ``default_factory`` at
        ``config/settings.py:281``, so the fallback only fires in tests
        with bare mocks.

        ``strategy1`` (intra_arbitrage) stays unconditionally enabled —
        ``BotConfig.strategy`` has no ``intra_arb_enabled`` flag today;
        the strategy's kill switch is upstream of BotConfig.
        """
        # Equity — risk.equity is a property on RiskManager
        try:
            equity_raw = getattr(risk, "equity", 0.0)
            equity: float = float(equity_raw) if equity_raw is not None else 0.0
        except (TypeError, ValueError):
            equity = 0.0

        # day_start_equity — on _RiskState, not a top-level property;
        # access via _state if available, fall back to equity.
        _state = getattr(risk, "_state", None)
        try:
            day_raw = getattr(_state, "day_start_equity", equity)
            day_start_equity: float = (
                float(day_raw) if day_raw is not None else equity
            )
        except (TypeError, ValueError):
            day_start_equity = equity

        # open_positions — dict[str, Position] on _state
        try:
            raw_positions: dict[str, Any] = dict(
                getattr(_state, "open_positions", {}) or {}
            )
        except TypeError:
            raw_positions = {}

        _strat_cfg = getattr(config, "strategy", None)
        momentum_enabled = bool(
            getattr(_strat_cfg, "momentum_enabled", True)
        )
        market_maker_enabled = bool(
            getattr(_strat_cfg, "market_maker_enabled", True)
        )

        return cls(
            equity=equity,
            day_start_equity=day_start_equity,
            open_positions=raw_positions,
            recent_fills=deque(maxlen=200),
            strategy_configs={
                "strategy1": StrategyRuntimeConfig(
                    strategyId="strategy1", enabled=True
                ),
                "strategy2": StrategyRuntimeConfig(
                    strategyId="strategy2",
                    enabled=momentum_enabled,
                ),
                "strategy3": StrategyRuntimeConfig(
                    strategyId="strategy3",
                    enabled=market_maker_enabled,
                ),
            },
            ws_subscribers=[],
            last_updated_at_ms=0,
        )

    # ------------------------------------------------------------------
    # Shallow copy for snapshot reads (D.2.1 §D-4)
    # ------------------------------------------------------------------

    def shallow_copy(self) -> "BotState":
        """
        Return a point-in-time snapshot copy of this BotState.

        Call this while holding write_lock, then release the lock and
        serialise the copy.  This minimises lock hold time (D.2.1 §D-4).

        Mutable collections are copied by value so the caller observes a
        stable snapshot even if the strategy loop mutates the originals
        after the lock is released.

        ws_subscribers is NOT copied — that list is always live.  The
        copy's ws_subscribers is set to an empty list so callers cannot
        accidentally broadcast on a snapshot copy.

        OQ-3 stub lists are shallow-copied.  They are empty during D.2.2/
        D.2.3; D.2.4 will populate them.
        """
        copy = BotState(
            equity=self.equity,
            day_start_equity=self.day_start_equity,
            open_positions=dict(self.open_positions),
            recent_fills=deque(self.recent_fills, maxlen=self.recent_fills.maxlen),
            strategy_configs={
                k: v.model_copy() for k, v in self.strategy_configs.items()
            },
            ws_subscribers=[],          # intentionally empty on copy
            kill_switch_requested=self.kill_switch_requested,
            last_updated_at_ms=self.last_updated_at_ms,
            risk_metrics=self.risk_metrics,
            equity_history=deque(
                self.equity_history, maxlen=self.equity_history.maxlen
            ),
            monthly_pnl=list(self.monthly_pnl),
            backtest=list(self.backtest),
            recent_logs=deque(self.recent_logs, maxlen=self.recent_logs.maxlen),
            recent_alerts=deque(
                self.recent_alerts, maxlen=self.recent_alerts.maxlen
            ),
            # New asyncio.Lock for the copy; the copy should not share
            # the original's lock state.
            _write_lock=asyncio.Lock(),
        )
        return copy

    # ------------------------------------------------------------------
    # Strategy config helpers
    # ------------------------------------------------------------------

    def get_strategy_config(self, strategy_id: StrategyId) -> StrategyRuntimeConfig:
        """
        Return the current runtime config for a strategy.
        Raises KeyError if strategy_id is not in strategy_configs
        (indicates a programming error, not a user error).
        """
        return self.strategy_configs[strategy_id]

    def apply_strategy_patch(self, patch: Any) -> None:
        """
        Apply a StrategyConfigPatch to the corresponding strategy config
        in-place.  Caller must hold write_lock if needed for atomicity.

        NOTE (v0.2.8 post-tag correction): an earlier version of this
        method broadcast a STRATEGY_UPDATE WsMessage to dashboard
        subscribers after applying the patch (introduced in d1d2276,
        reverted here). That wiring was contract-nonconformant:
        BOT_CONTRACT.md:609 specifies STRATEGY_UPDATE.updates as a
        partial of StrategyCardData (card-telemetry fields), not
        StrategyConfigPatch (runtime-config fields). The bot emitted
        config-patch fields, the dashboard zod parser stripped them as
        unknown keys, and the reducer applied an empty delta — silent
        no-op. The correct emit site is the strategy-telemetry
        pipeline (v0.2.9). See docs/audit/d24-emit-site-audit.md §6.0
        (three strategy surfaces) and §10 (correction block).
        """
        current = self.strategy_configs[patch.strategyId]
        self.strategy_configs[patch.strategyId] = current.apply_patch(patch)
