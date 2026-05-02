import asyncio
import os
import sys
import types as types_mod
import pytest

# Stub py_clob_client so unit tests can construct live CLOBExecutor without the
# optional wheel (CI / minimal environments). Real installs still work.
def _install_fake_py_clob_client() -> None:
    if "py_clob_client.client" in sys.modules:
        return

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


def _ensure_py_clob_client_stub() -> None:
    try:
        import py_clob_client.client  # noqa: F401
    except ImportError:
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
