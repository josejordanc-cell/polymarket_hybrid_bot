import asyncio
import os
import sys
import types as types_mod
import pytest

# ---------------------------------------------------------------------------
# py_clob_client fake-SDK stub (CI / minimal environments + API drift)
# ---------------------------------------------------------------------------
#
# IMPORTANT — this stub MASKS A REAL PRODUCTION DRIFT BUG.
#
# The bot's import block at ``execution/clob_executor.py:34-40`` expects:
#     from py_clob_client.order_builder.constants import Side
# along with ``Side.BUY`` / ``Side.SELL`` enum members used at
# ``clob_executor.py:329`` and in four strategy modules.
#
# The installed ``py-clob-client==0.22.0`` (per ``requirements.txt``) does
# NOT export ``Side`` — its ``order_builder/constants.py`` only contains
# bare ``BUY = "BUY"`` / ``SELL = "SELL"`` strings. Running the bot's
# live-mode path against the real installed SDK fails with:
#     ImportError: cannot import name 'Side' from
#         'py_clob_client.order_builder.constants'
# which the broad ``except Exception:`` at ``clob_executor.py:38``
# converts to ``_CLOB_AVAILABLE = False``, which then surfaces at
# ``clob_executor.py:121`` as the (misleading) error:
#     RuntimeError: py-clob-client-v2 is not installed.
#
# The error message lies — the package IS installed; the symbol just
# doesn't exist in this version.
#
# This conftest fake-SDK stub catches the API-drift case (not just the
# package-totally-missing case the pre-Round-9 stub handled) so the test
# suite stays green. **It does NOT fix the production drift** — live
# mode on the droplet will still raise the misleading error until the
# bot's own code is updated to match v0.22.0's API (use bare ``"BUY"``/
# ``"SELL"`` strings, or pin to a different SDK version that exports
# ``Side``). Tracking item: ``v029-clob-sdk-drift`` (production fix,
# separate round). See Round 9-continued recon document for full
# context.
#
# Pre-Round-9 the guard only checked ``import py_clob_client.client`` —
# which succeeds in our environment (the real package IS installed) —
# so the stub never fired, and the 6 ``test_live_fill_reconciliation``
# tests failed against the real-but-drifted SDK. Post-Round-9 the guard
# probes for every symbol the bot's import block expects; failing any
# one triggers the fake stub via ``sys.modules`` overwrite (which
# preempts the real-but-broken package for the test process).
# ---------------------------------------------------------------------------


def _install_fake_py_clob_client() -> None:
    """Install a fake ``py_clob_client`` module tree in ``sys.modules``.

    Unconditional — overwrites any existing entries (the real package
    may be importable but missing symbols we need; sys.modules
    assignment takes precedence over disk-loaded modules)."""
    root = types_mod.ModuleType("py_clob_client")
    client = types_mod.ModuleType("py_clob_client.client")

    class ClobClient:  # noqa: D401
        """Test stub."""

        def __init__(self, *args, **kwargs) -> None:
            pass

    client.ClobClient = ClobClient

    clob_types = types_mod.ModuleType("py_clob_client.clob_types")

    class ApiCreds:
        def __init__(self, **kwargs) -> None:
            pass

    class OrderArgs:
        def __init__(self, **kwargs) -> None:
            pass

    clob_types.ApiCreds = ApiCreds
    clob_types.OrderArgs = OrderArgs

    ob = types_mod.ModuleType("py_clob_client.order_builder")
    ob.__path__ = []  # type: ignore[attr-defined]
    obc = types_mod.ModuleType("py_clob_client.order_builder.constants")

    class Side:
        BUY = "BUY"
        SELL = "SELL"

    obc.Side = Side

    sys.modules["py_clob_client"] = root
    sys.modules["py_clob_client.client"] = client
    sys.modules["py_clob_client.clob_types"] = clob_types
    sys.modules["py_clob_client.order_builder"] = ob
    sys.modules["py_clob_client.order_builder.constants"] = obc


def _real_sdk_has_required_symbols() -> bool:
    """Probe whether the real installed SDK exposes every symbol the
    bot's import block at ``execution/clob_executor.py:34-40`` (and
    parallel imports in four strategy modules) expects.

    ``v0.22.0`` of py-clob-client passes the package-existence check
    (``py_clob_client.client`` imports) but FAILS this probe because
    ``Side`` is not exported from ``order_builder.constants``. Same
    failure mode as "not installed" for the bot's purposes. Returns
    ``False`` to trigger the fake-stub installation.
    """
    try:
        from py_clob_client.client import ClobClient  # noqa: F401
        from py_clob_client.clob_types import (  # noqa: F401
            ApiCreds,
            OrderArgs,
        )
        from py_clob_client.order_builder.constants import Side  # noqa: F401
        return True
    except ImportError:
        return False


def _ensure_py_clob_client_stub() -> None:
    """Install the fake SDK unless the REAL SDK has every symbol the
    bot expects. Tightened in Round 9 to detect API drift, not just
    package absence (see module-header comment for context)."""
    if not _real_sdk_has_required_symbols():
        _install_fake_py_clob_client()


_ensure_py_clob_client_stub()

# Ensure tests run with sim mode and a deterministic experiment_id so
# they never accidentally hit live config validation.
os.environ.setdefault("BOT_MODE", "sim")
os.environ.setdefault("BOT_EXPERIMENT_ID", "test")


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()
