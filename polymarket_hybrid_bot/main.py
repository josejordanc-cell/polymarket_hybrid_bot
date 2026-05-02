"""
Docker / module entry: ``python -m polymarket_hybrid_bot.main``.

Delegates to the repository-root ``main.py`` so strategy code stays at the top level.
"""
from __future__ import annotations

import runpy
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


if __name__ == "__main__":
    runpy.run_path(str(_ROOT / "main.py"), run_name="__main__")
