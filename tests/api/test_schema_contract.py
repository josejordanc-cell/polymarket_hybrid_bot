"""
tests/api/test_schema_contract.py — schema-contract regression test.

Snapshots the bot's WsAlert and WsFill envelope shapes against a
hardcoded expectation derived from the *current* dashboard
WsMessageSchema (components/dashboard/schemas.ts:215). The job of this
test is to fail loudly if either side's envelope shape drifts so that
the schema-drift class of bug we hit in D.2.4 step 1 (and §10's
discovery narrative) can be caught at unit-test time rather than at
integration time.

Scope as of this batch (D.2.4 step 2):
  - ALERT: bot WsAlert.payload == AlertItem (flat, post-ALERT-batch shape)
  - FILL : bot WsFill.payload  == FillRecord (flat, post-FILL-batch shape)

STRATEGY_UPDATE coverage is deferred to the D.2.4 step 3 batch, when
WsStrategyUpdatePayload is reconciled to match dashboard
StrategyUpdateSchema. Until then this file intentionally does not
snapshot WsStrategyUpdate.

The snapshots below capture the dashboard contract as it stood when
this test was written. If the dashboard side moves first, both this
snapshot and the bot's pydantic schemas should change together in a
paired commit.
"""
from __future__ import annotations

from api.schemas import AlertItem, FillRecord, WsAlert, WsFill


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
