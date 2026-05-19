"""
tests/scripts/test_trigger_sim_fill.py

Tests for scripts/trigger_sim_fill.py — the Phase E.2 sim-mode fill
trigger used to verify B.1 FILL strategy-id translation end-to-end.

Three test cases:
1. test_cli_parses_default_args     — argparse Namespace has documented defaults.
2. test_invalid_strategy_fails_fast — bogus --strategy raises ValueError.
3. test_strategy_translation_in_broadcast — broadcast_fill is awaited once with
   the internal strategy name (translation happens inside broadcast_fill at
   utils/fills.py:108, not in the script).

Async-mock pattern mirrors tests/utils/test_fills.py.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.trigger_sim_fill import build_parser, main, _emit


# ---------------------------------------------------------------------------
# 1. Default argument parsing
# ---------------------------------------------------------------------------

def test_cli_parses_default_args() -> None:
    """Invoking the parser with no args produces the documented defaults."""
    parser = build_parser()
    args = parser.parse_args([])

    assert args.strategy == "intra_arbitrage"
    assert args.venue == "polymarket"
    assert args.side == "buy"
    assert args.size_usd == 100.0
    assert args.price == 0.5
    assert args.token_id == "sim-token-0000000000000000"


# ---------------------------------------------------------------------------
# 2. Invalid strategy fails fast
# ---------------------------------------------------------------------------

def test_invalid_strategy_fails_fast() -> None:
    """A bogus --strategy value raises ValueError before any broadcast attempt."""
    parser = build_parser()
    args = parser.parse_args(["--strategy", "garbage"])

    with pytest.raises(ValueError, match="No dashboard contract id"):
        # _emit calls bot_to_dashboard which raises ValueError for unknown names.
        import asyncio
        asyncio.run(_emit(args))


# ---------------------------------------------------------------------------
# 3. broadcast_fill is called with the internal strategy name
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_strategy_translation_in_broadcast() -> None:
    """main() with --strategy intra_arbitrage calls broadcast_fill exactly once
    with strategy='intra_arbitrage' (the internal name).  The dashboard
    contract-id translation (strategy1) happens INSIDE broadcast_fill at
    utils/fills.py:108 — not in the script itself — so the script passes the
    internal name through unchanged."""
    with patch(
        "scripts.trigger_sim_fill.broadcast_fill",
        new_callable=AsyncMock,
    ) as mock_broadcast:
        parser = build_parser()
        args = parser.parse_args(["--strategy", "intra_arbitrage"])
        await _emit(args)

    mock_broadcast.assert_awaited_once()
    call_kwargs = mock_broadcast.await_args.kwargs
    assert call_kwargs["strategy"] == "intra_arbitrage"
