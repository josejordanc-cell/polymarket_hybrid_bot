"""
tests/api/test_schema_contract.py — schema-contract regression test.

Snapshots the bot's WsAlert and WsFill envelope shapes against a
hardcoded expectation derived from the *current* dashboard
WsMessageSchema (components/dashboard/schemas.ts:215). The job of this
test is to fail loudly if either side's envelope shape drifts so that
the schema-drift class of bug we hit in D.2.4 step 1 (and §10's
discovery narrative) can be caught at unit-test time rather than at
integration time.

Scope as of this batch (D.2.4 step 3):
  - ALERT           : bot WsAlert.payload           == AlertItem      (flat, post-ALERT-batch shape)
  - FILL            : bot WsFill.payload            == FillRecord     (flat, post-FILL-batch shape)
  - STRATEGY_UPDATE : bot WsStrategyUpdate.payload  == StrategyUpdate (flat, post-step-3 shape)

STRATEGY_UPDATE has two invariants: (a) the envelope-flatness
invariant parallel to ALERT/FILL, and (b) a field-set assertion that
the bot's StrategyUpdateFields MATCHES the contract's StrategyCardData
partial per BOT_CONTRACT.md:413, 609. Assertion is against the literal
contract text (the constant in this file is copy-pasted from
BOT_CONTRACT.md), NOT against the bot's own pydantic model — see the
v0.2.8 post-tag correction in d24-emit-site-audit.md §6.0 (three
strategy surfaces) and §10 (correction block) for the discipline
rationale.

The snapshots below capture the dashboard contract as it stood when
this test was written. If the dashboard side moves first, both this
snapshot and the bot's pydantic schemas should change together in a
paired commit.
"""
from __future__ import annotations

from api.schemas import (
    AlertItem,
    FillRecord,
    StrategyUpdate,
    StrategyUpdateFields,
    WsAlert,
    WsFill,
    WsStrategyUpdate,
)


# Hardcoded snapshot of dashboard AlertItemSchema field-set
# (components/dashboard/schemas.ts:163 as of D.2.4 step 1 landing).
# Field names + their JSON-schema type. Keys not types (avoiding
# brittle exact-pydantic-output matching).
_DASH_ALERT_FIELDS = {
    "id": "string",
    "time": "string",
    "severity": "string",  # enum literal in dashboard; appears as string in pydantic json schema
    "source": "string",
    "message": "string",
}

# Hardcoded snapshot of dashboard FillSchema field-set
# (components/dashboard/schemas.ts:73 as of D.2.4 step 2 landing).
# pnl is recorded as "nullable" in this snapshot because the paired
# dashboard commit makes it `z.number().nullable()`. See d24-emit-site-audit.md §10.
_DASH_FILL_FIELDS = {
    "id": "string",
    "time": "string",
    "symbol": "string",
    "action": "string",  # enum literal "UP"|"DOWN"
    "price": "number",
    "size": "number",
    "pnl": "nullable-number",
    "status": "string",  # enum literal "filled"|"pending"
    "strategy": "string",
}


def _pydantic_field_names(model_cls) -> set[str]:
    """Return the set of field names declared on a pydantic v2 model."""
    return set(model_cls.model_fields.keys())


def test_alert_item_field_set_matches_dashboard_snapshot() -> None:
    """If AlertItem gains/loses fields without a paired dashboard commit,
    the field-set mismatch fails here."""
    bot = _pydantic_field_names(AlertItem)
    expected = set(_DASH_ALERT_FIELDS.keys())
    assert bot == expected, (
        f"AlertItem fields drifted from dashboard snapshot. "
        f"Bot has: {sorted(bot)}; expected (dashboard): {sorted(expected)}. "
        "If dashboard changed, update the snapshot in this file AND verify "
        "components/dashboard/schemas.ts:163 matches."
    )


def test_ws_alert_envelope_is_flat() -> None:
    """WsAlert must wrap AlertItem directly (no nested .alert key).
    The D.2.3-era WsAlertPayload nesting was the drift that the §10
    discovery narrative is about; this guards against regression."""
    bot = _pydantic_field_names(WsAlert)
    # Envelope is exactly {type, payload}; payload is AlertItem directly.
    assert bot == {"type", "payload"}, (
        f"WsAlert envelope shape drifted. Has: {sorted(bot)}. "
        "Expected {'type', 'payload'} with payload: AlertItem (flat, not nested)."
    )
    payload_ann = WsAlert.model_fields["payload"].annotation
    assert payload_ann is AlertItem, (
        f"WsAlert.payload annotation is {payload_ann!r}, expected AlertItem. "
        "If a wrapper class was reintroduced, see d24-emit-site-audit.md §10."
    )


def test_fill_record_field_set_matches_dashboard_snapshot() -> None:
    """If FillRecord gains/loses fields without a paired dashboard commit,
    the field-set mismatch fails here."""
    bot = _pydantic_field_names(FillRecord)
    expected = set(_DASH_FILL_FIELDS.keys())
    assert bot == expected, (
        f"FillRecord fields drifted from dashboard snapshot. "
        f"Bot has: {sorted(bot)}; expected (dashboard): {sorted(expected)}. "
        "If dashboard changed, update the snapshot in this file AND verify "
        "components/dashboard/schemas.ts:73 matches."
    )


def test_ws_fill_envelope_is_flat() -> None:
    """WsFill must wrap FillRecord directly (no nested .fill key),
    matching dashboard WsMessageSchema (schemas.ts:217)."""
    bot = _pydantic_field_names(WsFill)
    assert bot == {"type", "payload"}, (
        f"WsFill envelope shape drifted. Has: {sorted(bot)}. "
        "Expected {'type', 'payload'} with payload: FillRecord (flat, not nested)."
    )
    payload_ann = WsFill.model_fields["payload"].annotation
    assert payload_ann is FillRecord, (
        f"WsFill.payload annotation is {payload_ann!r}, expected FillRecord."
    )


def test_fill_record_pnl_is_optional() -> None:
    """pnl is Optional[float] (nullable) per the Level-1 decision in
    d24-emit-site-audit.md §10: the bot does not know realized pnl at
    fill time. The dashboard's FillSchema.pnl is z.number().nullable()
    to match."""
    pnl_field = FillRecord.model_fields["pnl"]
    # pydantic v2: Optional[T] means the field is not required.
    assert pnl_field.is_required() is False, (
        "FillRecord.pnl must be Optional/not-required so the bot can emit "
        "None at fill time (dashboard FillSchema.pnl is nullable to match). "
        "See d24-emit-site-audit.md §10."
    )


# Hardcoded snapshot of the contract-truth STRATEGY_UPDATE.updates
# field-set per BOT_CONTRACT.md:413, 609. Source of truth: the
# dashboard's StrategyCardUpdatesSchema =
# StrategyCardDataSchema.omit({id: true}).partial() at
# components/dashboard/schemas.ts:211. The bot's StrategyUpdateFields
# MUST match this set exactly — STRATEGY_UPDATE carries card-telemetry
# deltas, not config-patch deltas.
#
# HISTORY (v0.2.8 post-tag correction): an earlier version of this
# test file (introduced d1d2276) defined a separate
# _BOT_STRATEGY_UPDATE_FIELDS = {enabled, maxPositionSize, riskBudget,
# activePairs} and rationalized the bot/dashboard disjoint-ness as
# "permissive superset, semantic not syntactic." That was wrong: the
# bot's emission was empirically a no-op on the dashboard side because
# zod's .partial() stripped all four unknown keys. The correct
# invariant is: bot field-set == contract field-set, asserted against
# the literal contract text. See d24-emit-site-audit.md §6.0 (three
# strategy surfaces) and §10 (correction block).
_CONTRACT_STRATEGY_UPDATE_FIELDS = {
    "name",
    "status",
    "pnlToday",
    "pnlMonth",
    "winRate",
    "tradesToday",
    "avgHoldMinutes",
    "lastSignal",
    "pipelineStep",
    "exposure",
    "sparkline",
}


def test_strategy_update_fields_match_contract_card_partial() -> None:
    """StrategyUpdateFields must match the contract-truth field-set:
    a partial of StrategyCardData per BOT_CONTRACT.md:413, 609.
    Allowed keys: name, status, pnlToday, pnlMonth, winRate, tradesToday,
    avgHoldMinutes, lastSignal, pipelineStep, exposure, sparkline.

    This test asserts against the LITERAL CONTRACT TEXT (the constant
    above is copy-pasted from BOT_CONTRACT.md:609), NOT against the
    bot's own pydantic model. Asserting bot-against-bot proves nothing —
    that's the self-consistent-but-wrong failure mode the d1d2276
    wiring fell into. See d24-emit-site-audit.md §10 (correction block,
    process note on contract-regression test discipline)."""
    bot = _pydantic_field_names(StrategyUpdateFields)
    expected = _CONTRACT_STRATEGY_UPDATE_FIELDS
    assert bot == expected, (
        f"StrategyUpdateFields drifted from contract field-set "
        f"(BOT_CONTRACT.md:609). "
        f"Bot has: {sorted(bot)}; expected: {sorted(expected)}. "
        "If the contract changed, update the snapshot above to match "
        "BOT_CONTRACT.md:609. If StrategyUpdateFields changed "
        "independently of the contract, stop and review — see "
        "d24-emit-site-audit.md §6.0 (three strategy surfaces) for "
        "the surface that STRATEGY_UPDATE should emit from."
    )


def test_strategy_update_payload_envelope_shape() -> None:
    """StrategyUpdate wraps {strategyId, updates: StrategyUpdateFields}.
    No nested wrapper class (parallel to TICK/FILL/ALERT flattening in §10)."""
    bot = _pydantic_field_names(StrategyUpdate)
    assert bot == {"strategyId", "updates"}, (
        f"StrategyUpdate payload shape drifted. Has: {sorted(bot)}. "
        "Expected {'strategyId', 'updates'} per dashboard StrategyUpdateSchema."
    )
    updates_ann = StrategyUpdate.model_fields["updates"].annotation
    assert updates_ann is StrategyUpdateFields, (
        f"StrategyUpdate.updates annotation is {updates_ann!r}, "
        "expected StrategyUpdateFields. If a wrapper class was introduced, "
        "see d24-emit-site-audit.md §10."
    )


def test_ws_strategy_update_envelope_is_flat() -> None:
    """WsStrategyUpdate must wrap StrategyUpdate directly (no
    WsStrategyUpdatePayload wrapper class — that was dropped in D.2.4
    step 3). Parallel to WsAlert/WsFill flattening invariants."""
    bot = _pydantic_field_names(WsStrategyUpdate)
    assert bot == {"type", "payload"}, (
        f"WsStrategyUpdate envelope shape drifted. Has: {sorted(bot)}. "
        "Expected {'type', 'payload'} with payload: StrategyUpdate (flat, not nested)."
    )
    payload_ann = WsStrategyUpdate.model_fields["payload"].annotation
    assert payload_ann is StrategyUpdate, (
        f"WsStrategyUpdate.payload annotation is {payload_ann!r}, "
        "expected StrategyUpdate. If WsStrategyUpdatePayload was reintroduced, "
        "see d24-emit-site-audit.md §10."
    )
