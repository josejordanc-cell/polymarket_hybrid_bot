"""tests/test_settlements_writer.py

Unit tests for utils/settlements.py (profitability epic PR2).
Targets persist_settlement() directly with tmp_path; mirrors
tests/test_trades_writer.py shape and conventions.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from utils.settlements import _resolve_settlements_path, persist_settlement


def _read_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_single_row_written(tmp_path: Path) -> None:
    out = tmp_path / "settlements.jsonl"
    persist_settlement(
        market_id="0xabc",
        resolved_outcome="YES",
        pnl=12.34,
        equity_after=1012.34,
        path=out,
    )
    rows = _read_lines(out)
    assert len(rows) == 1
    row = rows[0]
    assert row["market_id"] == "0xabc"
    assert row["resolved_outcome"] == "YES"
    assert row["pnl"] == pytest.approx(12.34)
    assert row["equity_after"] == pytest.approx(1012.34)
    assert row["winning"] is True
    assert row["ts"].endswith("Z")
    assert "T" in row["ts"]


def test_winning_field_matches_pnl_sign(tmp_path: Path) -> None:
    """winning = pnl > 0; break-even (pnl == 0) → False."""
    out = tmp_path / "settlements.jsonl"
    # Win
    persist_settlement(
        market_id="0xa", resolved_outcome="YES", pnl=5.0, equity_after=1005.0, path=out
    )
    # Loss
    persist_settlement(
        market_id="0xb", resolved_outcome="NO", pnl=-3.0, equity_after=1002.0, path=out
    )
    # Break-even
    persist_settlement(
        market_id="0xc", resolved_outcome="YES", pnl=0.0, equity_after=1002.0, path=out
    )
    rows = _read_lines(out)
    assert [r["winning"] for r in rows] == [True, False, False]


def test_appends_does_not_overwrite(tmp_path: Path) -> None:
    out = tmp_path / "settlements.jsonl"
    for i in range(5):
        persist_settlement(
            market_id=f"0x{i}",
            resolved_outcome="YES" if i % 2 == 0 else "NO",
            pnl=float(i - 2),  # -2, -1, 0, 1, 2
            equity_after=1000.0 + float(i - 2),
            path=out,
        )
    rows = _read_lines(out)
    assert len(rows) == 5
    assert [r["market_id"] for r in rows] == [f"0x{i}" for i in range(5)]


def test_missing_parent_dir_created(tmp_path: Path) -> None:
    out = tmp_path / "nested" / "deep" / "settlements.jsonl"
    assert not out.parent.exists()
    persist_settlement(
        market_id="0xabc",
        resolved_outcome="YES",
        pnl=1.0,
        equity_after=1001.0,
        path=out,
    )
    assert out.exists()
    assert len(_read_lines(out)) == 1


def test_permission_error_swallowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed open() must log and return None, not raise."""
    out = tmp_path / "settlements.jsonl"

    def _boom(*_args, **_kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "open", _boom)
    # Must not raise
    persist_settlement(
        market_id="0xabc",
        resolved_outcome="YES",
        pnl=1.0,
        equity_after=1001.0,
        path=out,
    )
    assert not out.exists()


def test_bot_data_dir_env_respected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BOT_DATA_DIR", str(tmp_path))
    path = _resolve_settlements_path()
    assert path == tmp_path / "settlements.jsonl"


def test_bot_data_dir_default_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BOT_DATA_DIR", raising=False)
    path = _resolve_settlements_path()
    assert path == Path("data") / "settlements.jsonl"


def test_default_path_writes_to_resolved_location(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When path=None, persist_settlement writes to
    BOT_DATA_DIR/settlements.jsonl as resolved."""
    monkeypatch.setenv("BOT_DATA_DIR", str(tmp_path))
    persist_settlement(
        market_id="0xabc", resolved_outcome="YES", pnl=2.0, equity_after=1002.0
    )
    out = tmp_path / "settlements.jsonl"
    assert out.is_file()
    assert len(_read_lines(out)) == 1
