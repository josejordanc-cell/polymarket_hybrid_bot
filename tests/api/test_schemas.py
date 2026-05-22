"""tests/api/test_schemas.py — Pydantic round-trip tests for api/schemas.py.

Pure unit tests: no asyncio, no aiohttp, no BotState, no risk_manager.
All models are constructed directly from api.schemas.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from api.schemas import (
    AlertItem,
    BotLogEntry,
    DashboardSnapshot,
    EquityPoint,
    FillRecord,
    GlobalStats,
    OpenPosition,
    PriceTick,
    RiskMetrics,
    StopResponse,
    StrategyCardData,
    StrategyConfig,
    StrategyConfigPatch,
    StrategyRuntimeConfig,
    bot_to_dashboard,
    dashboard_to_bot,
)

# ---------------------------------------------------------------------------
# Section: GlobalStats
# ---------------------------------------------------------------------------


def test_globalstats_round_trip() -> None:
    """v0.2.9 contract: 8 required fields per dashboard GlobalStatsSchema."""
    stats = GlobalStats(
        portfolioValue=12500.0,
        pnlToday=300.0,
        pnlMonth=1200.0,
        pnlAllTime=4800.0,
        winRate=0.6,
        totalTrades=42,
        streak=3,
        lastUpdatedAt=1700000000000,
    )
    data = stats.model_dump(mode="json")
    assert isinstance(data["portfolioValue"], float)
    assert isinstance(data["totalTrades"], int)
    assert isinstance(data["streak"], int)
    assert isinstance(data["lastUpdatedAt"], int)
    assert data["pnlMonth"] == 1200.0
    assert data["pnlAllTime"] == 4800.0
    # Removed fields must not appear in the dumped payload.
    assert "totalEquity" not in data
    assert "dayPnl" not in data
    assert "dayPnlPct" not in data
    assert "openPositions" not in data
    assert "sharpeRatio" not in data
    assert "maxDrawdown" not in data


def test_globalstats_rejects_missing_required_field() -> None:
    """v0.2.9 contract: all 8 fields required. Omitting portfolioValue
    (the renamed totalEquity) must raise ValidationError; the old-shape
    keyword `totalEquity` is silently dropped by pydantic v2's default
    extras-ignore policy, so the failure surfaces as 'portfolioValue
    missing' rather than 'totalEquity unknown'."""
    with pytest.raises(ValidationError):
        GlobalStats(
            totalEquity=12500.0,   # old-shape key — ignored
            dayPnl=300.0,           # old-shape key — ignored
            pnlMonth=0.0,
            pnlAllTime=0.0,
            winRate=0.6,
            totalTrades=42,
            streak=0,
            lastUpdatedAt=1700000000000,
            # portfolioValue + pnlToday omitted → ValidationError
        )


# ---------------------------------------------------------------------------
# Section: StrategyCardData
# ---------------------------------------------------------------------------


def test_strategycarddata_valid_ids() -> None:
    for sid in ("strategy1", "strategy2", "strategy3"):
        card = StrategyCardData(
            id=sid,
            name=f"Strategy {sid[-1]}",
            status="live",
            pnlToday=100.0,
            winRate=0.6,
            tradesToday=10,
            pipelineStep=1,
        )
        assert card.id == sid


def test_strategycarddata_invalid_id() -> None:
    with pytest.raises(ValidationError):
        StrategyCardData(
            id="strategy4",
            name="Unknown",
            status="live",
            pipelineStep=4,
        )


def test_strategycarddata_invalid_status() -> None:
    with pytest.raises(ValidationError):
        StrategyCardData(
            id="strategy1",
            name="Intra Arb",
            status="running",
            pipelineStep=1,
        )


# ---------------------------------------------------------------------------
# Section: OpenPosition
# ---------------------------------------------------------------------------


def test_openposition_round_trip() -> None:
    """v0.2.9 3d-fix: canonical 11-field Zod shape per
    components/dashboard/schemas.ts:67 + BOT_CONTRACT.md v1.0 line 115.
    direction is title-case Long|Short; timeOpen is ISO 8601 UTC with
    Z suffix."""
    pos = OpenPosition(
        id="MKT-BTC-001:YES",
        tradeId="MKT-BTC-001:YES",
        symbol="MKT-BTC-001",
        strategy="strategy1",
        direction="Long",
        entryPrice=0.55,
        currentPrice=0.60,
        size=50.0,
        unrealizedPnlPct=9.09,
        unrealizedPnlUsd=2.5,
        timeOpen="2026-05-21T13:00:00Z",
    )
    data = pos.model_dump(mode="json")
    pos2 = OpenPosition.model_validate(data)
    assert pos2.id == "MKT-BTC-001:YES"
    assert pos2.tradeId == "MKT-BTC-001:YES"
    assert pos2.symbol == "MKT-BTC-001"
    assert pos2.strategy == "strategy1"
    assert pos2.direction == "Long"
    assert pos2.entryPrice == 0.55
    assert pos2.currentPrice == 0.60
    assert pos2.size == 50.0
    assert pos2.unrealizedPnlPct == 9.09
    assert pos2.unrealizedPnlUsd == 2.5
    assert pos2.timeOpen == "2026-05-21T13:00:00Z"
    # Legacy field names from f9d1314 (the wrong audit row B.3 shape)
    # must NOT round-trip via the model dump.
    for legacy in (
        "side",          # superseded by direction (title case)
        "quantity",      # superseded by size
        "openedAt",      # superseded by timeOpen (ISO 8601 UTC)
        "unrealizedPnl", # superseded by unrealizedPnlUsd
        "marketId",      # internal-only; collapsed into id
        "outcome",       # internal-only; collapsed into id
        "avgPrice",      # superseded by entryPrice
        "unrealisedPnl", # legacy British-English spelling
    ):
        assert legacy not in data


def test_openposition_direction_is_title_case() -> None:
    """v0.2.9 3d-fix: BOT_CONTRACT.md v1.0 line 472 mandates title case
    Long|Short for direction. Lowercase long/short (from the f9d1314
    landing) must be rejected."""
    with pytest.raises(ValidationError):
        OpenPosition(
            id="pos-bad",
            tradeId="pos-bad",
            symbol="MKT-X",
            strategy="strategy1",
            direction="long",  # lowercase no longer valid
            entryPrice=0.5,
            currentPrice=0.5,
            size=10.0,
            unrealizedPnlPct=0.0,
            unrealizedPnlUsd=0.0,
            timeOpen="2026-05-21T13:00:00Z",
        )


def test_openposition_rejects_legacy_field_set() -> None:
    """v0.2.9 3d-fix: the f9d1314 shape (side, quantity, openedAt,
    unrealizedPnl as float epoch-ms timeOpen) is no longer accepted.
    Per pydantic v2 default extras-ignore policy these keys are
    silently dropped; construction fails because the new required
    fields are missing. Documents the practical effect for
    downstream callers."""
    with pytest.raises(ValidationError):
        OpenPosition(
            id="pos-bad",
            side="long",                  # f9d1314 key — ignored
            quantity=10.0,                 # f9d1314 key — ignored
            openedAt=1700000000000,        # f9d1314 key — ignored
            unrealizedPnl=0.0,             # f9d1314 key — ignored
            symbol="MKT-X",
            strategy="strategy1",
            entryPrice=0.5,
            currentPrice=0.5,
            unrealizedPnlPct=0.0,
            # tradeId, direction, size, unrealizedPnlUsd, timeOpen
            # all omitted → ValidationError
        )


# ---------------------------------------------------------------------------
# Section: FillRecord
# ---------------------------------------------------------------------------


def test_fillrecord_round_trip() -> None:
    fill = FillRecord(
        id="fill-999",
        time="16:30:00",
        symbol="MKT-XYZ-001",
        action="DOWN",
        price=0.38,
        size=20.0,
        strategy="strategy3",
        pnl=-1.2,
    )
    data = fill.model_dump(mode="json")
    fill2 = FillRecord.model_validate(data)
    assert fill2.id == fill.id
    assert fill2.action == fill.action
    assert fill2.strategy == fill.strategy
    assert fill2.pnl == fill.pnl
    # status defaults to "filled" per dashboard contract; verify it survives round-trip.
    assert fill2.status == "filled"


def test_fillrecord_optional_fields_none() -> None:
    """pnl is Optional[float] per the dashboard-canonical-broke-here decision
    documented in docs/audit/d24-emit-site-audit.md §10. Verify that
    constructing with pnl=None succeeds and survives round-trip."""
    fill = FillRecord(
        id="fill-000",
        time="16:31:00",
        symbol="MKT-BTC-001",
        action="UP",
        price=0.70,
        size=5.0,
        strategy="strategy1",
        pnl=None,
    )
    data = fill.model_dump()
    assert "pnl" in data
    assert data["pnl"] is None


# ---------------------------------------------------------------------------
# Section: RiskMetrics
# ---------------------------------------------------------------------------


def test_riskmetrics_default_shape() -> None:
    """v0.2.9 3e: canonical 6-field Zod shape per
    components/dashboard/schemas.ts:96 + BOT_CONTRACT.md v1.0 line 119.
    Zero-arg construction yields the wire-default object: leverage=1.0
    (spot-venue structural constant), other five fields = None."""
    rm = RiskMetrics()
    data = rm.model_dump(mode="json")

    # Exact 6 keys in Zod order.
    expected_keys = [
        "maxDrawdown",
        "sharpe",
        "var95",
        "leverage",
        "exposurePct",
        "liquidationRisk",
    ]
    assert list(data.keys()) == expected_keys

    # leverage is a structural constant; everything else is null by default.
    assert data["leverage"] == 1.0
    assert isinstance(data["leverage"], float)
    assert data["maxDrawdown"] is None
    assert data["sharpe"] is None
    assert data["var95"] is None
    assert data["exposurePct"] is None
    assert data["liquidationRisk"] is None


def test_riskmetrics_leverage_overridable_but_constant_in_production() -> None:
    """v0.2.9 3e: leverage is a regular Pydantic field with a default of
    1.0, not a frozen attribute. The override path exists for completeness
    (tests, future leveraged-venue addition), but production callers must
    rely on the default — Polymarket binary spot is structurally
    non-leveraged. This test documents the override capability and the
    production invariant in one place."""
    rm = RiskMetrics(leverage=2.5)
    assert rm.leverage == 2.5  # override works
    # Documentary assertion: zero-arg construction (production path)
    # always yields leverage=1.0.
    assert RiskMetrics().leverage == 1.0


def test_riskmetrics_liquidationrisk_enum() -> None:
    """v0.2.9 3e: liquidationRisk is an enum (Literal["low","medium",
    "high"]), NOT a number. Matches Zod's z.enum([...]).nullable()."""
    for valid in ("low", "medium", "high"):
        rm = RiskMetrics(liquidationRisk=valid)
        assert rm.liquidationRisk == valid

    # Null is the default and also explicitly accepted.
    assert RiskMetrics(liquidationRisk=None).liquidationRisk is None

    # Invalid values must be rejected.
    with pytest.raises(ValidationError):
        RiskMetrics(liquidationRisk="invalid")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        RiskMetrics(liquidationRisk="LOW")  # type: ignore[arg-type]  # case-sensitive
    with pytest.raises(ValidationError):
        RiskMetrics(liquidationRisk=0.5)  # type: ignore[arg-type]  # wrong type


def test_riskmetrics_rejects_legacy_fields() -> None:
    """v0.2.9 3e: the v0.2.8 legacy field set (currentDrawdown,
    sharpeRatio, kellyFraction, pairCorrelation, dailyVaR) is no longer
    part of the model. Per pydantic v2 default extras-ignore policy
    these kwargs are silently dropped — construction still succeeds,
    but the legacy keys must NOT appear in the dumped payload."""
    rm = RiskMetrics(
        # Legacy keys from the 5bc2b6b risk-stub — silently dropped.
        currentDrawdown=0.03,
        sharpeRatio=1.5,
        kellyFraction=0.25,
        pairCorrelation=0.5,
        dailyVaR=100.0,
    )
    data = rm.model_dump()
    for legacy in (
        "currentDrawdown",
        "sharpeRatio",
        "kellyFraction",
        "pairCorrelation",
        "dailyVaR",
    ):
        assert legacy not in data
        assert not hasattr(rm, legacy)

    # The 6 canonical fields are still present in the dump.
    for canonical in (
        "maxDrawdown",
        "sharpe",
        "var95",
        "leverage",
        "exposurePct",
        "liquidationRisk",
    ):
        assert canonical in data


def test_riskmetrics_round_trip_with_real_values() -> None:
    """v0.2.9 3e: round-trip a fully populated RiskMetrics (anticipates
    B.5 full-wiring producing real values for the five nullable
    fields)."""
    rm = RiskMetrics(
        maxDrawdown=5.2,
        sharpe=1.4,
        var95=180.0,
        leverage=1.0,
        exposurePct=12.5,
        liquidationRisk="medium",
    )
    data = rm.model_dump(mode="json")
    rm2 = RiskMetrics.model_validate(data)
    assert rm2.maxDrawdown == 5.2
    assert rm2.sharpe == 1.4
    assert rm2.var95 == 180.0
    assert rm2.leverage == 1.0
    assert rm2.exposurePct == 12.5
    assert rm2.liquidationRisk == "medium"


# ---------------------------------------------------------------------------
# Section: StrategyConfig + StrategyConfigPatch
# ---------------------------------------------------------------------------


def test_strategyconfig_valid() -> None:
    cfg = StrategyConfig(
        strategyId="strategy1",
        enabled=True,
        maxPositionSize=100.0,
        riskPct=2.5,
    )
    assert cfg.strategyId == "strategy1"
    assert cfg.enabled is True
    assert cfg.riskPct == 2.5


def test_strategyconfig_maxpositionsize_below_min() -> None:
    """v0.2.9 (L2 E.2): maxPositionSize range 0.1–100.0."""
    with pytest.raises(ValidationError):
        StrategyConfig(
            strategyId="strategy1",
            enabled=True,
            maxPositionSize=0.0,
            riskPct=2.5,
        )


def test_strategyconfig_maxpositionsize_above_max() -> None:
    """v0.2.9 (L2 E.2): maxPositionSize must not exceed 100.0."""
    with pytest.raises(ValidationError):
        StrategyConfig(
            strategyId="strategy1",
            enabled=True,
            maxPositionSize=150.0,
            riskPct=2.5,
        )


def test_strategyconfig_riskpct_in_range() -> None:
    """v0.2.9 (L2 E.2): riskPct 2.5 (= 2.5%) is the new sensible default."""
    cfg = StrategyConfig(
        strategyId="strategy1",
        enabled=True,
        maxPositionSize=100.0,
        riskPct=2.5,
    )
    assert cfg.riskPct == 2.5


def test_strategyconfig_riskpct_rejects_below_0_1() -> None:
    """v0.2.9 (L2 E.2): riskPct lower bound 0.1 (= 0.1%)."""
    with pytest.raises(ValidationError):
        StrategyConfig(
            strategyId="strategy1",
            enabled=True,
            maxPositionSize=100.0,
            riskPct=0.05,
        )


def test_strategyconfig_riskpct_rejects_above_10() -> None:
    """v0.2.9 (L2 E.2): riskPct upper bound 10.0 (= 10%)."""
    with pytest.raises(ValidationError):
        StrategyConfig(
            strategyId="strategy1",
            enabled=True,
            maxPositionSize=100.0,
            riskPct=10.5,
        )


def test_strategyconfig_activepairs_array_format() -> None:
    """v0.2.9 (L2 E.2): activePairs is a list of BASE/QUOTE strings.
    The old CSV string form ('BTC/USDT,ETH/USDT') must NOT be accepted."""
    cfg = StrategyConfig(
        strategyId="strategy2",
        enabled=True,
        maxPositionSize=50.0,
        riskPct=2.5,
        activePairs=["BTC/USDT", "ETH/USDT"],
    )
    assert cfg.activePairs == ["BTC/USDT", "ETH/USDT"]

    # Old CSV form must be rejected — pydantic v2 does not coerce
    # 'BTC/USDT,ETH/USDT' into a list, and per-element regex would fail
    # even if it did.
    with pytest.raises(ValidationError):
        StrategyConfig(
            strategyId="strategy2",
            enabled=True,
            maxPositionSize=50.0,
            riskPct=2.5,
            activePairs="BTC/USDT,ETH/USDT",  # type: ignore[arg-type]
        )


def test_strategyconfig_activepairs_regex_rejects_invalid() -> None:
    """v0.2.9 (L2 E.2): each element must match BASE/QUOTE."""
    with pytest.raises(ValidationError):
        StrategyConfig(
            strategyId="strategy2",
            enabled=True,
            maxPositionSize=50.0,
            riskPct=2.5,
            activePairs=["btcusdt"],
        )

    with pytest.raises(ValidationError):
        StrategyConfig(
            strategyId="strategy2",
            enabled=True,
            maxPositionSize=50.0,
            riskPct=2.5,
            activePairs=["BTC-USDT"],  # hyphen instead of slash
        )


def test_strategyconfig_rejects_id_keyword() -> None:
    """v0.2.9 (L2 E.2): 'id' is no longer a field on StrategyConfig.
    Per pydantic v2 default (extra='ignore'), passing id=... is silently
    dropped — so construction fails because the new required field
    strategyId is missing. This documents the practical effect of the
    rename for downstream callers."""
    with pytest.raises(ValidationError):
        StrategyConfig(
            id="strategy1",           # old key — silently dropped
            enabled=True,
            maxPositionSize=100.0,
            riskPct=2.5,
            # strategyId omitted → ValidationError
        )


def test_strategyconfigpatch_all_none() -> None:
    patch = StrategyConfigPatch(strategyId="strategy1")
    assert patch.enabled is None
    assert patch.maxPositionSize is None
    assert patch.riskPct is None
    assert patch.activePairs is None


# ---------------------------------------------------------------------------
# Section: DashboardSnapshot
# ---------------------------------------------------------------------------


def test_dashboardsnapshot_all_optional() -> None:
    """v0.2.9 audit row A.1: six top-level keys were renamed
    (openPositions→positions, recentFills→fills, riskMetrics→risk,
    equityHistory→equity, recentLogs→logs, recentAlerts→alerts)."""
    snap = DashboardSnapshot()
    assert snap.globalStats is None
    assert snap.strategies is None
    assert snap.positions is None
    assert snap.fills is None
    assert snap.strategyConfigs is None
    assert snap.risk is None
    assert snap.equity is None
    assert snap.monthlyPnl is None
    assert snap.backtest is None
    assert snap.logs is None
    assert snap.alerts is None


def test_dashboardsnapshot_top_level_keys_match_contract() -> None:
    """v0.2.9 audit row A.1: the six new wire-key names must exist on the
    model, and none of the six legacy names must remain as either an
    attribute or a dumped-payload key."""
    snap = DashboardSnapshot()
    # New keys are present as attributes.
    for new_key in ("positions", "fills", "risk", "equity", "logs", "alerts"):
        assert hasattr(snap, new_key), f"missing new key {new_key!r}"

    # Legacy keys are absent as attributes.
    for legacy_key in (
        "openPositions",
        "recentFills",
        "riskMetrics",
        "equityHistory",
        "recentLogs",
        "recentAlerts",
    ):
        assert not hasattr(snap, legacy_key), (
            f"legacy key {legacy_key!r} must not exist on DashboardSnapshot"
        )

    # Legacy keys are absent from the dumped payload too (belt-and-braces;
    # pydantic v2 default extras-ignore would silently drop them).
    data = snap.model_dump(mode="json")
    for legacy_key in (
        "openPositions",
        "recentFills",
        "riskMetrics",
        "equityHistory",
        "recentLogs",
        "recentAlerts",
    ):
        assert legacy_key not in data


def test_dashboardsnapshot_no_top_level_lastUpdatedAt() -> None:
    """v0.2.9 audit row A.2: lastUpdatedAt was relocated from
    DashboardSnapshot root into globalStats. The model must reject the
    attribute at the snapshot root level (pydantic v2 default
    extras-ignore policy means it's silently dropped, but the attribute
    must no longer exist on the model)."""
    snap = DashboardSnapshot()
    assert not hasattr(snap, "lastUpdatedAt")
    data = snap.model_dump(mode="json")
    assert "lastUpdatedAt" not in data


def test_dashboardsnapshot_partial() -> None:
    gs = GlobalStats(
        portfolioValue=10000.0,
        pnlToday=100.0,
        pnlMonth=400.0,
        pnlAllTime=1600.0,
        winRate=0.55,
        totalTrades=20,
        streak=0,
        lastUpdatedAt=1700000500000,
    )
    snap = DashboardSnapshot(globalStats=gs)
    data = snap.model_dump(mode="json", exclude_none=True)
    assert list(data.keys()) == ["globalStats"]


def test_dashboardsnapshot_full_round_trip() -> None:
    """v0.2.9 contract: lastUpdatedAt lives inside globalStats, not at root."""
    gs = GlobalStats(
        portfolioValue=15000.0,
        pnlToday=500.0,
        pnlMonth=2000.0,
        pnlAllTime=6000.0,
        winRate=0.6,
        totalTrades=30,
        streak=4,
        lastUpdatedAt=1700001000000,
    )
    fill = FillRecord(
        id="fill-rt-01",
        time="16:32:00",
        symbol="MKT-BTC-001",
        action="UP",
        price=0.65,
        size=10.0,
        strategy="strategy1",
    )
    snap = DashboardSnapshot(
        globalStats=gs,
        fills=[fill],
    )
    data = snap.model_dump(mode="json")
    # lastUpdatedAt must live inside globalStats, not at the snapshot root.
    assert "lastUpdatedAt" not in data
    assert data["globalStats"]["lastUpdatedAt"] == 1700001000000
    snap2 = DashboardSnapshot.model_validate(data)
    assert snap2.globalStats is not None
    assert snap2.globalStats.lastUpdatedAt == 1700001000000
    assert snap2.globalStats.portfolioValue == 15000.0
    assert snap2.fills is not None
    assert len(snap2.fills) == 1


# ---------------------------------------------------------------------------
# Section: StrategyRuntimeConfig
# ---------------------------------------------------------------------------


def test_strategyruntimeconfig_defaults() -> None:
    """v0.2.9 (L2 E.2): default riskPct=2.5 (= 2.5%) replaces the prior
    riskBudget=0.33 — see commit message for the 33%→2.5% rationale."""
    cfg = StrategyRuntimeConfig(strategyId="strategy1")
    assert cfg.enabled is True
    assert cfg.maxPositionSize == 100.0
    assert cfg.riskPct == 2.5
    assert cfg.activePairs is None


def test_strategyruntimeconfig_apply_patch() -> None:
    base = StrategyRuntimeConfig(strategyId="strategy1")
    patch = StrategyConfigPatch(
        strategyId="strategy1", enabled=False, maxPositionSize=75.0
    )
    updated = base.apply_patch(patch)
    assert updated.enabled is False
    assert updated.maxPositionSize == 75.0
    assert updated.riskPct == 2.5  # default unchanged
    # strategyId is excluded from the patch payload — it identifies the
    # target config, not a mutation.
    assert updated.strategyId == "strategy1"


def test_strategyruntimeconfig_to_contract() -> None:
    cfg = StrategyRuntimeConfig(
        strategyId="strategy2",
        enabled=True,
        maxPositionSize=50.0,
        riskPct=1.5,
        activePairs=["BTC/USDT"],
    )
    contract = cfg.to_contract()
    assert isinstance(contract, StrategyConfig)
    assert contract.strategyId == cfg.strategyId
    assert contract.enabled == cfg.enabled
    assert contract.maxPositionSize == cfg.maxPositionSize
    assert contract.riskPct == cfg.riskPct
    assert contract.activePairs == cfg.activePairs


# ---------------------------------------------------------------------------
# Section: StopResponse
# ---------------------------------------------------------------------------


def test_stopresponse_default() -> None:
    resp = StopResponse()
    assert resp.ok is True
    assert resp.stoppedAt is None


def test_stopresponse_with_ts() -> None:
    resp = StopResponse(ok=True, stoppedAt=1700000000000)
    data = resp.model_dump(mode="json")
    resp2 = StopResponse.model_validate(data)
    assert resp2.ok is True
    assert resp2.stoppedAt == 1700000000000
    assert isinstance(resp2.stoppedAt, int)


# ---------------------------------------------------------------------------
# Section: bot_to_dashboard / dashboard_to_bot helpers (B.1)
# ---------------------------------------------------------------------------


def test_bot_to_dashboard_intra_arbitrage() -> None:
    assert bot_to_dashboard("intra_arbitrage") == "strategy1"


def test_bot_to_dashboard_momentum_latency() -> None:
    assert bot_to_dashboard("momentum_latency") == "strategy2"


def test_bot_to_dashboard_market_maker() -> None:
    assert bot_to_dashboard("market_maker") == "strategy3"


def test_bot_to_dashboard_smart_money_copy_raises() -> None:
    with pytest.raises(ValueError) as exc_info:
        bot_to_dashboard("smart_money_copy")
    assert "smart_money_copy" in str(exc_info.value)
    assert "v1.0 contract" in str(exc_info.value)


def test_bot_to_dashboard_garbage_raises() -> None:
    with pytest.raises(ValueError):
        bot_to_dashboard("garbage")


def test_dashboard_to_bot_strategy1() -> None:
    assert dashboard_to_bot("strategy1") == "intra_arbitrage"


def test_dashboard_to_bot_strategy2() -> None:
    assert dashboard_to_bot("strategy2") == "momentum_latency"


def test_dashboard_to_bot_strategy3() -> None:
    assert dashboard_to_bot("strategy3") == "market_maker"


def test_dashboard_to_bot_strategy4_raises() -> None:
    with pytest.raises(ValueError) as exc_info:
        dashboard_to_bot("strategy4")
    assert "strategy4" in str(exc_info.value)


@pytest.mark.parametrize("internal_name", [
    "intra_arbitrage",
    "momentum_latency",
    "market_maker",
])
def test_round_trip_identity(internal_name: str) -> None:
    """dashboard_to_bot(bot_to_dashboard(x)) == x for all three mapped names."""
    assert dashboard_to_bot(bot_to_dashboard(internal_name)) == internal_name


# ---------------------------------------------------------------------------
# Section: EquityPoint (v0.2.9 3f — audit row B.7)
# ---------------------------------------------------------------------------


def test_equitypoint_default_shape() -> None:
    """v0.2.9 3f: canonical 3-field Zod shape per
    components/dashboard/schemas.ts:22 + BOT_CONTRACT.md v1.0 line 597.
    Exact key order: day, equity, pnl."""
    pt = EquityPoint(day="2026-05-21", equity=10250.50, pnl=125.50)
    data = pt.model_dump(mode="json")
    assert list(data.keys()) == ["day", "equity", "pnl"]
    assert data["day"] == "2026-05-21"
    assert data["equity"] == 10250.50
    assert data["pnl"] == 125.50
    assert isinstance(data["day"], str)
    assert isinstance(data["equity"], float)
    assert isinstance(data["pnl"], float)


def test_equitypoint_round_trip() -> None:
    pt = EquityPoint(day="2026-05-21", equity=10000.0, pnl=0.0)
    data = pt.model_dump(mode="json")
    pt2 = EquityPoint.model_validate(data)
    assert pt2.day == pt.day
    assert pt2.equity == pt.equity
    assert pt2.pnl == pt.pnl


def test_equitypoint_rejects_legacy_ts_field() -> None:
    """v0.2.9 3f: the prior (ts: int, equity: float) shape was wrong.
    Per pydantic v2 default extras-ignore policy, ``ts=`` is silently
    dropped — construction fails because ``day`` and ``pnl`` are
    missing required fields."""
    with pytest.raises(ValidationError):
        EquityPoint(
            ts=1700000000000,    # legacy key — silently dropped
            equity=10000.0,
            # day + pnl omitted → ValidationError
        )

    # When all 3 canonical fields are present, ts= is silently dropped
    # and construction succeeds; ts must NOT appear in the dumped payload.
    pt = EquityPoint(
        ts=1700000000000,        # legacy key — silently dropped
        day="2026-05-21",
        equity=10000.0,
        pnl=0.0,
    )
    data = pt.model_dump()
    assert "ts" not in data


# ---------------------------------------------------------------------------
# Section: BotLogEntry (v0.2.9 3f — audit row B.8 / Option C lossy map)
# ---------------------------------------------------------------------------


def test_botlogentry_default_shape() -> None:
    """v0.2.9 3f: canonical 4-field Zod shape per
    components/dashboard/schemas.ts:110 + BOT_CONTRACT.md v1.0 line 123.
    Exact key order: id, time, level, message."""
    entry = BotLogEntry(
        id="log-001",
        time="16:30:00",
        level="INFO",
        message="bot started",
    )
    data = entry.model_dump(mode="json")
    assert list(data.keys()) == ["id", "time", "level", "message"]
    assert data["id"] == "log-001"
    assert data["time"] == "16:30:00"
    assert data["level"] == "INFO"
    assert data["message"] == "bot started"


def test_botlogentry_round_trip() -> None:
    entry = BotLogEntry(
        id="log-042",
        time="17:01:33",
        level="WARN",
        message="something noteworthy",
    )
    data = entry.model_dump(mode="json")
    entry2 = BotLogEntry.model_validate(data)
    assert entry2.id == entry.id
    assert entry2.time == entry.time
    assert entry2.level == entry.level
    assert entry2.message == entry.message


def test_botlogentry_level_enum() -> None:
    """v0.2.9 3f Option C: level is uppercase 4-tier
    Literal["INFO","WARN","ERROR","EXEC"]. Lowercase Python-logging
    values (debug/info/warning/error/critical) are NOT accepted at the
    Pydantic layer — those are internal-side and pass through
    _map_log_level in api/routes_rest.py before wire-construction.

    Bot NEVER produces "EXEC" today (vestigial slot), but the Pydantic
    model accepts it so future test fixtures or schema validators can
    construct it without bypass."""
    # Accept all four canonical wire literals.
    for level in ("INFO", "WARN", "ERROR", "EXEC"):
        entry = BotLogEntry(
            id="log-x",
            time="00:00:00",
            level=level,
            message="x",
        )
        assert entry.level == level

    # Reject lowercase Python-logging tier (these flow through
    # _map_log_level upstream of the model).
    for invalid in ("info", "warning", "error", "critical", "debug",
                    "Info", "DEBUG"):
        with pytest.raises(ValidationError):
            BotLogEntry(
                id="log-bad",
                time="00:00:00",
                level=invalid,  # type: ignore[arg-type]
                message="x",
            )

    # Reject unknown literals.
    with pytest.raises(ValidationError):
        BotLogEntry(
            id="log-bad",
            time="00:00:00",
            level="TRACE",  # type: ignore[arg-type]
            message="x",
        )


def test_botlogentry_rejects_legacy_fields() -> None:
    """v0.2.9 3f: the prior bot model had (ts: int, level: lowercase,
    message: str, context: Optional[dict]). The new shape drops ``ts``
    (replaced by ``time: str``) and ``context`` (no wire equivalent).
    Pydantic v2 extras-ignore drops legacy kwargs silently — construction
    fails because the new required fields are missing."""
    with pytest.raises(ValidationError):
        BotLogEntry(
            ts=1700000000000,          # legacy key — silently dropped
            level="info",                # legacy lowercase — also wrong shape
            message="x",
            context={"k": "v"},          # legacy key — silently dropped
            # id + time + uppercase level all missing → ValidationError
        )


# ---------------------------------------------------------------------------
# Section: AlertItem (v0.2.9 3f — audit row B.9 / E.3 severity collapse)
# ---------------------------------------------------------------------------


def test_alertitem_default_shape() -> None:
    """v0.2.9 3f: canonical 5-field Zod shape per
    components/dashboard/schemas.ts:174 + BOT_CONTRACT.md v1.0 line 125.
    Exact key order: id, time, severity, source, message. (Schema was
    already aligned pre-3f; docstring updated for severity-collapse
    policy.)"""
    alert = AlertItem(
        id="alert-001",
        time="16:30:00",
        severity="warning",
        source="risk_manager",
        message="drawdown nearing limit",
    )
    data = alert.model_dump(mode="json")
    assert list(data.keys()) == ["id", "time", "severity", "source", "message"]
    assert data["id"] == "alert-001"
    assert data["time"] == "16:30:00"
    assert data["severity"] == "warning"
    assert data["source"] == "risk_manager"
    assert data["message"] == "drawdown nearing limit"


def test_alertitem_round_trip() -> None:
    alert = AlertItem(
        id="alert-002",
        time="17:00:00",
        severity="critical",
        source="kill_switch",
        message="daily drawdown limit breached",
    )
    data = alert.model_dump(mode="json")
    alert2 = AlertItem.model_validate(data)
    assert alert2.id == alert.id
    assert alert2.time == alert.time
    assert alert2.severity == alert.severity
    assert alert2.source == alert.source
    assert alert2.message == alert.message


def test_alertitem_severity_enum() -> None:
    """v0.2.9 3f E.3 collapse: severity is lowercase 3-tier
    Literal["info","warning","critical"]. Internal "error" tier
    collapses to "critical" via _map_alert_severity upstream of the
    Pydantic layer; "debug" is dropped entirely."""
    # Accept all three canonical wire literals.
    for sev in ("info", "warning", "critical"):
        alert = AlertItem(
            id="x",
            time="00:00:00",
            severity=sev,
            source="x",
            message="x",
        )
        assert alert.severity == sev

    # Reject uppercase (those are BotLogEntry levels, not AlertItem
    # severities — the two enums collide in casing).
    with pytest.raises(ValidationError):
        AlertItem(
            id="x",
            time="00:00:00",
            severity="INFO",  # type: ignore[arg-type]
            source="x",
            message="x",
        )

    # Reject internal-tier values that flow through _map_alert_severity
    # upstream of the model.
    for invalid in ("error", "debug", "ERROR", "Critical"):
        with pytest.raises(ValidationError):
            AlertItem(
                id="x",
                time="00:00:00",
                severity=invalid,  # type: ignore[arg-type]
                source="x",
                message="x",
            )


# ---------------------------------------------------------------------------
# Section: PriceTick (v0.2.9 3i — audit row B.14)
# ---------------------------------------------------------------------------


def test_pricetick_default_shape() -> None:
    """v0.2.9 3i: canonical 5-field Zod shape per
    components/dashboard/schemas.ts:211 + BOT_CONTRACT.md v1.0.
    Exact key order: symbol, last, bid, ask, ts."""
    tick = PriceTick(
        symbol="BTC/USDT",
        last=77800.0,
        bid=77795.0,
        ask=77805.0,
        ts=1716302400000,
    )
    data = tick.model_dump(mode="json")
    assert list(data.keys()) == ["symbol", "last", "bid", "ask", "ts"]
    assert data["symbol"] == "BTC/USDT"
    assert data["last"] == 77800.0
    assert data["bid"] == 77795.0
    assert data["ask"] == 77805.0
    assert data["ts"] == 1716302400000
    assert isinstance(data["ts"], int)  # epoch ms, NOT float seconds


def test_pricetick_required_fields_only() -> None:
    """v0.2.9 3i policy lock 4 + 6: bid/ask default to None in v1.
    Per 3i policy lock 5, ts is always populated by the bot — the
    Optional in the model is for forward-compatibility / parse robustness
    only."""
    tick = PriceTick(symbol="BTC/USD", last=77800.0)
    assert tick.symbol == "BTC/USD"
    assert tick.last == 77800.0
    assert tick.bid is None
    assert tick.ask is None
    assert tick.ts is None


def test_pricetick_bid_ask_explicit_none() -> None:
    """v0.2.9 3i: bot ships bid=None / ask=None explicitly today (the
    spot feed discards ccxt bid/ask at ingestion). Verify the model
    round-trips that representation cleanly."""
    tick = PriceTick(symbol="ETH/USDT", last=3400.0, bid=None, ask=None, ts=1716302400000)
    data = tick.model_dump(mode="json")
    assert data["bid"] is None
    assert data["ask"] is None
    # Round-trip via model_validate.
    tick2 = PriceTick.model_validate(data)
    assert tick2.bid is None
    assert tick2.ask is None


def test_pricetick_ts_is_13_digit_epoch_ms() -> None:
    """v0.2.9 3i policy lock 5 + Team A 3g .refine(): ts is epoch
    milliseconds (13-digit, ~1.7e12). Document the unit explicitly
    so regression catches any drift back to epoch seconds."""
    # Representative 2024-05-21 epoch ms.
    ts_ms = 1716302400000
    tick = PriceTick(symbol="BTC/USDT", last=77800.0, ts=ts_ms)
    assert tick.ts == ts_ms
    # Sanity: within Team A's 3g refine() range [1e12, 1e14).
    assert 1_000_000_000_000 <= tick.ts < 100_000_000_000_000


def test_pricetick_last_must_be_positive() -> None:
    """v0.2.9 3i: last is a price; gt=0 enforced at the model layer
    (defense-in-depth on top of the feed's own ``price > 0`` guard at
    data_feeds/spot_feed.py:98)."""
    with pytest.raises(ValidationError):
        PriceTick(symbol="BTC/USDT", last=0.0)
    with pytest.raises(ValidationError):
        PriceTick(symbol="BTC/USDT", last=-1.0)


def test_pricetick_rejects_legacy_fields() -> None:
    """v0.2.9 3i: the pre-3i shape (marketId, price, ts: Optional[float]
    seconds) is no longer accepted. Per pydantic v2 default
    extras-ignore policy, legacy kwargs are silently dropped — so
    construction fails because the new required fields (symbol, last)
    are missing."""
    with pytest.raises(ValidationError):
        PriceTick(
            marketId="0xabc",          # legacy key — silently dropped
            price=77800.0,             # legacy key — silently dropped
            ts=1.716302400e9,          # legacy: epoch SECONDS as float
            # symbol + last omitted → ValidationError
        )

    # When the new required fields are present, legacy keys are silently
    # dropped and the dump does not contain them.
    tick = PriceTick(
        marketId="0xabc",              # legacy — dropped
        price=999.0,                   # legacy — dropped
        symbol="BTC/USDT",
        last=77800.0,
    )
    data = tick.model_dump()
    assert "marketId" not in data
    assert "price" not in data
