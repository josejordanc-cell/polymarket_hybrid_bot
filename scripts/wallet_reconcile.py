#!/usr/bin/env python3
"""Ad-hoc CLI: Polygon USDC vs bot equity. Run from repo root: python scripts/wallet_reconcile.py --help"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from utils.wallet_reconcile_cli import main

if __name__ == "__main__":
    raise SystemExit(main())
