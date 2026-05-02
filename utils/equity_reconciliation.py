"""
On-chain Polygon USDC vs bot equity reconciliation (Prompt 9).

Compares wallet USDC (bridged and/or native) to RiskManager equity or CLI-supplied
equity from /metrics or log scrape. Does not include pUSD — Polymarket collateral
may be wrapped; this check is strictly ERC-20 USDC balances on the EOA.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from web3 import Web3

# Polymarket CollateralOnramp docs use USDC.e (bridged) as a primary onramp asset;
# Circle native USDC on Polygon is widely used for deposits. Production wallets may
# hold either or both — we query both and combine per resolve_polygon_usdc_human().
USDC_POLYGON_BRIDGED = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # USDC.e (PoS bridge)
USDC_POLYGON_NATIVE = "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359"  # native USDC (Circle)

ERC20_MIN_ABI: list[dict[str, Any]] = [
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "type": "function",
    },
]

_LOG = logging.getLogger(__name__)


def derive_wallet_address(private_key: str) -> str:
    from eth_account import Account

    pk = (private_key or "").strip()
    if not pk:
        raise ValueError("empty private key")
    if not pk.startswith("0x"):
        pk = "0x" + pk
    return Account.from_key(pk).address


def get_polygon_wallet_address(config: Any) -> str:
    """Checksum address from BOT_POLYGON_WALLET_ADDRESS or Polymarket private key."""
    from web3 import Web3

    override = str(getattr(config, "polygon_wallet_address", "") or "").strip()
    if override:
        return Web3.to_checksum_address(override)
    pk = getattr(config.api, "polymarket_private_key", "") if hasattr(config, "api") else ""
    if not pk:
        raise ValueError(
            "Set BOT_POLYGON_WALLET_ADDRESS or BOT_POLYMARKET_PRIVATE_KEY to derive the wallet"
        )
    return Web3.to_checksum_address(derive_wallet_address(pk))


def _erc20_balance_human(w3: Web3, token_hex: str, wallet: str) -> float:
    c = w3.eth.contract(address=w3.to_checksum_address(token_hex), abi=ERC20_MIN_ABI)
    raw = c.functions.balanceOf(w3.to_checksum_address(wallet)).call()
    dec = int(c.functions.decimals().call())
    return float(raw) / float(10**dec)


def resolve_polygon_usdc_human(
    w3: Web3,
    wallet: str,
    warn: Callable[[str, dict[str, Any]], None] | None = None,
) -> tuple[float, str]:
    """
    Return (total USDC human amount, source tag).

    If exactly one of bridged/native is non-zero, use that balance.
    If both are non-zero, log a warning (wallet holds both) and return the sum.
    If both are zero, return (0.0, 'none').
    """
    bridged = _erc20_balance_human(w3, USDC_POLYGON_BRIDGED, wallet)
    native = _erc20_balance_human(w3, USDC_POLYGON_NATIVE, wallet)
    eps = 1e-9
    b_pos = bridged > eps
    n_pos = native > eps

    if b_pos and n_pos:
        msg = (
            "wallet holds both USDC.e and native USDC on Polygon; "
            "using combined balance for reconciliation"
        )
        if warn:
            warn(msg, {"bridged_usdc": bridged, "native_usdc": native})
        else:
            _LOG.warning(msg, extra={"bridged_usdc": bridged, "native_usdc": native})
        return bridged + native, "bridged_plus_native"

    if b_pos:
        return bridged, "bridged_usdc_e"
    if n_pos:
        return native, "native_usdc"
    return 0.0, "none"


def fetch_polygon_usdc_human(w3: Web3, wallet: str) -> tuple[float, str]:
    """Public wrapper for RPC balance fetch (single try/except at caller)."""
    return resolve_polygon_usdc_human(w3, wallet, warn=None)


def compute_drift_pct(on_chain_usdc: float, bot_equity_usd: float) -> tuple[float, float]:
    """Return (drift_usd, drift_pct) where drift_pct is signed percent (e.g. 5.0 == 5%)."""
    drift_usd = on_chain_usdc - bot_equity_usd
    if bot_equity_usd > 1e-9:
        drift_pct = 100.0 * drift_usd / bot_equity_usd
    else:
        drift_pct = 0.0 if abs(drift_usd) < 1e-9 else 100.0 * (1.0 if drift_usd > 0 else -1.0)
    return drift_usd, drift_pct


def parse_bot_equity_from_metrics_text(
    metrics_text: str,
    experiment_id: str,
    mode: str,
) -> float | None:
    from prometheus_client.parser import text_string_to_metric_families

    want_xp = str(experiment_id)
    want_mode = str(mode)
    best: float | None = None
    for family in text_string_to_metric_families(metrics_text):
        for sample in family.samples:
            if sample.name != "bot_equity_usd":
                continue
            labels = sample.labels or {}
            if labels.get("experiment_id") == want_xp and labels.get("mode") == want_mode:
                return float(sample.value)
            best = float(sample.value)
    return best


def fetch_bot_equity_from_metrics(
    port: int,
    experiment_id: str,
    mode: str,
    host: str = "127.0.0.1",
    timeout_sec: float = 5.0,
) -> float | None:
    url = f"http://{host}:{port}/metrics"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        _LOG.debug("metrics_fetch_failed", error=str(exc))
        return None
    return parse_bot_equity_from_metrics_text(body, experiment_id, mode)


def scrape_latest_equity_from_log_file(log_path: str) -> float | None:
    """Best-effort: last equity from JSON structlog lines (fill_recorded, settlement, etc.)."""
    last: float | None = None
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ev = obj.get("event")
                if ev == "fill_recorded" and "equity" in obj:
                    last = float(obj["equity"])
                elif ev == "settlement" and "equity" in obj:
                    last = float(obj["equity"])
                elif ev == "market_resolved" and "equity_after" in obj:
                    last = float(obj["equity_after"])
                elif ev == "bot_shutting_down" and "equity" in obj:
                    last = float(obj["equity"])
    except OSError as exc:
        _LOG.debug("log_scrape_failed", path=log_path, error=str(exc))
        return None
    return last


@dataclass
class DriftEvaluation:
    drift_usd: float
    drift_pct: float
    breaches_threshold: bool

    @classmethod
    def from_balances(
        cls,
        on_chain_usdc: float,
        bot_equity_usd: float,
        threshold_pct: float,
    ) -> DriftEvaluation:
        du, dp = compute_drift_pct(on_chain_usdc, bot_equity_usd)
        return cls(
            drift_usd=du,
            drift_pct=dp,
            breaches_threshold=abs(dp) > threshold_pct,
        )


async def maybe_alert_equity_drift(
    *,
    drift_pct: float,
    drift_usd: float,
    on_chain_usdc: float,
    bot_equity_usd: float,
    threshold_pct: float,
    send_alert: Any,
) -> bool:
    """If |drift_pct| > threshold, alert. Returns True if alert was sent."""
    if abs(drift_pct) <= threshold_pct:
        return False
    msg = (
        f"Equity drift {drift_pct:+.2f}% (threshold {threshold_pct}%) | "
        f"on-chain USDC ${on_chain_usdc:,.2f} vs bot ${bot_equity_usd:,.2f} "
        f"(Δ ${drift_usd:+,.2f})"
    )
    await send_alert(msg, level="warning")
    return True
