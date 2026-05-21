"""tests/api/test_schemas.py — Pydantic round-trip tests for api/schemas.py.

Pure unit tests: no asyncio, no aiohttp, no BotState, no risk_manager.
All models are constructed directly from api.schemas.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from api.schemas import (
    DashboardSnapshot,
    FillRecord,
    GlobalStats,
    OpenPosition,
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
    pos = OpenPosition(
        id="pos-001",
        marketId="MKT-BTC-001",
        outcome="YES",
        direction="Long",
        quantity=50.0,
        avgPrice=0.55,
        currentPrice=0.60,
        unrealisedPnl=2.5,
        strategy="strategy2",
        openedAt=1700000000000,
    )
    data = pos.model_dump(mode="json")
    pos2 = OpenPosition.model_validate(data)
    assert pos2.id == pos.id
    assert pos2.outcome == pos.outcome
    assert pos2.direction == pos.direction
    assert pos2.strategy == pos.strategy
    assert pos2.quantity == pos.quantity
    assert pos2.avgPrice == pos.avgPrice
    assert pos2.openedAt == pos.openedAt


def test_openposition_invalid_outcome() -> None:
    with pytest.raises(ValidationError):
        OpenPosition(
            id="pos-bad",
            marketId="MKT-BTC-001",
            outcome="MAYBE",
            direction="Long",
            quantity=10.0,
            avgPrice=0.5,
            strategy="strategy1",
            openedAt=1700000000000,
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


def test_riskmetrics_required_fields() -> None:
    # missing maxDrawdown
    with pytest.raises(ValidationError):
        RiskMetrics(currentDrawdown=0.05)

    # missing currentDrawdown
    with pytest.raises(ValidationError):
        RiskMetrics(maxDrawdown=0.10)


def test_riskmetrics_all_optional_none() -> None:
    rm = RiskMetrics(maxDrawdown=0.10, currentDrawdown=0.03)
    assert rm.sharpeRatio is None
    assert rm.kellyFraction is None
    assert rm.pairCorrelation is None
    assert rm.dailyVaR is None


# ---------------------------------------------------------------------------
# Section: StrategyConfig + StrategyConfigPatch
# ---------------------------------------------------------------------------


def test_strategyconfig_valid() -> None:
    cfg = StrategyConfig(
        id="strategy1",
        enabled=True,
        maxPositionSize=100.0,
        riskBudget=0.33,
    )
    assert cfg.id == "strategy1"
    assert cfg.enabled is True


def test_strategyconfig_maxpositionsize_zero() -> None:
    with pytest.raises(ValidationError):
        StrategyConfig(
            id="strategy1",
            enabled=True,
            maxPositionSize=0.0,
            riskBudget=0.33,
        )


def test_strategyconfig_riskbudget_out_of_range() -> None:
    with pytest.raises(ValidationError):
        StrategyConfig(
            id="strategy1",
            enabled=True,
            maxPositionSize=100.0,
            riskBudget=1.5,
        )


def test_strategyconfig_activepairs_valid() -> None:
    cfg = StrategyConfig(
        id="strategy2",
        enabled=True,
        maxPositionSize=50.0,
        riskBudget=0.25,
        activePairs="BTC-USD,ETH-USD",
    )
    assert cfg.activePairs == "BTC-USD,ETH-USD"


def test_strategyconfig_activepairs_invalid() -> None:
    with pytest.raises(ValidationError):
        StrategyConfig(
            id="strategy2",
            enabled=True,
            maxPositionSize=50.0,
            riskBudget=0.25,
            activePairs="btc-usd",
        )


def test_strategyconfigpatch_all_none() -> None:
    patch = StrategyConfigPatch(id="strategy1")
    assert patch.enabled is None
    assert patch.maxPositionSize is None
    assert patch.riskBudget is None
    assert patch.activePairs is None


# ---------------------------------------------------------------------------
# Section: DashboardSnapshot
# ---------------------------------------------------------------------------


def test_dashboardsnapshot_all_optional() -> None:
    snap = DashboardSnapshot()
    assert snap.globalStats is None
    assert snap.strategies is None
    assert snap.openPositions is None
    assert snap.recentFills is None
    assert snap.strategyConfigs is None
    assert snap.riskMetrics is None
    assert snap.equityHistory is None
    assert snap.monthlyPnl is None
    assert snap.backtest is None
    assert snap.recentLogs is None
    assert snap.recentAlerts is None


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
        recentFills=[fill],
    )
    data = snap.model_dump(mode="json")
    # lastUpdatedAt must live inside globalStats, not at the snapshot root.
    assert "lastUpdatedAt" not in data
    assert data["globalStats"]["lastUpdatedAt"] == 1700001000000
    snap2 = DashboardSnapshot.model_validate(data)
    assert snap2.globalStats is not None
    assert snap2.globalStats.lastUpdatedAt == 1700001000000
    assert snap2.globalStats.portfolioValue == 15000.0
    assert snap2.recentFills is not None
    assert len(snap2.recentFills) == 1


# ---------------------------------------------------------------------------
# Section: StrategyRuntimeConfig
# ---------------------------------------------------------------------------


def test_strategyruntimeconfig_defaults() -> None:
    cfg = StrategyRuntimeConfig(id="strategy1")
    assert cfg.enabled is True
    assert cfg.maxPositionSize == 100.0
    assert cfg.riskBudget == 0.33
    assert cfg.activePairs is None


def test_strategyruntimeconfig_apply_patch() -> None:
    base = StrategyRuntimeConfig(id="strategy1")
    patch = StrategyConfigPatch(id="strategy1", enabled=False, maxPositionSize=75.0)
    updated = base.apply_patch(patch)
    assert updated.enabled is False
    assert updated.maxPositionSize == 75.0
    assert updated.riskBudget == 0.33  # unchanged


def test_strategyruntimeconfig_to_contract() -> None:
    cfg = StrategyRuntimeConfig(
        id="strategy2",
        enabled=True,
        maxPositionSize=50.0,
        riskBudget=0.25,
        activePairs="BTC-USD",
    )
    contract = cfg.to_contract()
    assert isinstance(contract, StrategyConfig)
    assert contract.id == cfg.id
    assert contract.enabled == cfg.enabled
    assert contract.maxPositionSize == cfg.maxPositionSize
    assert contract.riskBudget == cfg.riskBudget
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
