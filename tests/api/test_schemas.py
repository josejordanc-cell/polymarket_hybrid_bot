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
)

# ---------------------------------------------------------------------------
# Section: GlobalStats
# ---------------------------------------------------------------------------


def test_globalstats_round_trip() -> None:
    stats = GlobalStats(
        totalEquity=12500.0,
        dayPnl=300.0,
        dayPnlPct=2.4,
        openPositions=3,
        totalTrades=42,
        winRate=0.6,
        sharpeRatio=None,
        maxDrawdown=None,
    )
    data = stats.model_dump(mode="json")
    assert isinstance(data["totalEquity"], float)
    assert isinstance(data["openPositions"], int)
    assert data["sharpeRatio"] is None
    assert data["maxDrawdown"] is None


# ---------------------------------------------------------------------------
# Section: StrategyCardData
# ---------------------------------------------------------------------------


def test_strategycarddata_valid_ids() -> None:
    for sid in ("strategy1", "strategy2", "strategy3"):
        card = StrategyCardData(
            id=sid,
            name=f"Strategy {sid[-1]}",
            status="active",
            pnl=100.0,
            pnlPct=1.0,
            trades=10,
            winRate=0.6,
            pipelineStep=1,
        )
        assert card.id == sid


def test_strategycarddata_invalid_id() -> None:
    with pytest.raises(ValidationError):
        StrategyCardData(
            id="strategy4",
            name="Unknown",
            status="active",
            pnl=0.0,
            pnlPct=0.0,
            trades=0,
            winRate=0.0,
            pipelineStep=4,
        )


def test_strategycarddata_invalid_status() -> None:
    with pytest.raises(ValidationError):
        StrategyCardData(
            id="strategy1",
            name="Intra Arb",
            status="running",
            pnl=0.0,
            pnlPct=0.0,
            trades=0,
            winRate=0.0,
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
        marketId="MKT-XYZ-001",
        outcome="NO",
        action="DOWN",
        quantity=20.0,
        price=0.38,
        strategy="strategy3",
        ts=1700001000000,
        fees=0.05,
        pnl=-1.2,
    )
    data = fill.model_dump(mode="json")
    fill2 = FillRecord.model_validate(data)
    assert fill2.id == fill.id
    assert fill2.action == fill.action
    assert fill2.strategy == fill.strategy
    assert fill2.fees == fill.fees
    assert fill2.pnl == fill.pnl


def test_fillrecord_optional_fields_none() -> None:
    fill = FillRecord(
        id="fill-000",
        marketId="MKT-BTC-001",
        outcome="YES",
        action="UP",
        quantity=5.0,
        price=0.70,
        strategy="strategy1",
        ts=1700002000000,
        fees=None,
        pnl=None,
    )
    data = fill.model_dump()
    assert "fees" in data
    assert data["fees"] is None
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
    assert snap.lastUpdatedAt is None


def test_dashboardsnapshot_partial() -> None:
    gs = GlobalStats(
        totalEquity=10000.0,
        dayPnl=100.0,
        dayPnlPct=1.0,
        openPositions=2,
        totalTrades=20,
        winRate=0.55,
    )
    snap = DashboardSnapshot(globalStats=gs)
    data = snap.model_dump(mode="json", exclude_none=True)
    assert list(data.keys()) == ["globalStats"]


def test_dashboardsnapshot_full_round_trip() -> None:
    gs = GlobalStats(
        totalEquity=15000.0,
        dayPnl=500.0,
        dayPnlPct=3.4,
        openPositions=1,
        totalTrades=30,
        winRate=0.6,
        sharpeRatio=1.2,
        maxDrawdown=0.05,
    )
    fill = FillRecord(
        id="fill-rt-01",
        marketId="MKT-BTC-001",
        outcome="YES",
        action="UP",
        quantity=10.0,
        price=0.65,
        strategy="strategy1",
        ts=1700000000000,
    )
    snap = DashboardSnapshot(
        globalStats=gs,
        recentFills=[fill],
        lastUpdatedAt=1700001000000,
    )
    data = snap.model_dump(mode="json")
    snap2 = DashboardSnapshot.model_validate(data)
    assert isinstance(snap2.lastUpdatedAt, int)
    assert snap2.lastUpdatedAt == 1700001000000
    assert snap2.globalStats is not None
    assert snap2.globalStats.totalEquity == 15000.0
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
