"""
data_feeds/polymarket_feed.py
Polymarket CLOB v2 WebSocket subscriber.

Maintains a live dict of MarketSnapshot keyed by token/market ID.
Also provides a helper to discover liquid BTC/ETH/SOL short-term markets.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

import structlog
import websockets
from websockets.exceptions import ConnectionClosed

from config.settings import BotConfig
from utils.types import MarketSnapshot, utc_now

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Resolution polling (Gamma REST — see main.py IMPLEMENTATION NOTE)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ResolutionEvent:
    market_id: str
    resolved_outcome: Literal["YES", "NO"]
    resolution_timestamp: datetime


def _parse_json_list(raw: Any) -> list[Any]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
            return data if isinstance(data, list) else []
        except json.JSONDecodeError:
            return []
    return []


def _outcome_label_to_yes_no(label: str, index: int) -> Literal["YES", "NO"]:
    s = label.strip().lower()
    if "yes" in s or s in {"y", "true"}:
        return "YES"
    if "no" in s or s in {"n", "false"}:
        return "NO"
    return "YES" if index == 0 else "NO"


def _parse_gamma_market_if_resolved(m: dict[str, Any]) -> ResolutionEvent | None:
    """Build ResolutionEvent if market is closed with a clear winning outcome."""
    mid = str(m.get("conditionId") or m.get("condition_id") or "").strip()
    if not mid or not m.get("closed"):
        return None

    outcomes = _parse_json_list(m.get("outcomes"))
    prices = _parse_json_list(m.get("outcomePrices"))
    if not outcomes or not prices:
        return None
    n = min(len(outcomes), len(prices))
    if n < 2:
        return None
    try:
        pfloat = [float(prices[i]) for i in range(n)]
    except (TypeError, ValueError):
        return None
    win_i = max(range(n), key=lambda i: pfloat[i])
    if pfloat[win_i] < 0.51:
        return None

    win_label = str(outcomes[win_i])
    resolved = _outcome_label_to_yes_no(win_label, win_i)

    ts_raw = m.get("closedTime") or m.get("closed_time") or m.get("endDateIso") or ""
    ts: datetime
    if isinstance(ts_raw, str) and ts_raw:
        try:
            iso = ts_raw.replace("Z", "+00:00")
            ts = datetime.fromisoformat(iso)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except ValueError:
            ts = utc_now()
    else:
        ts = utc_now()

    return ResolutionEvent(
        market_id=mid,
        resolved_outcome=resolved,
        resolution_timestamp=ts,
    )


# Maximum reconnect back-off (seconds)
_MAX_BACKOFF = 30.0


class PolymarketFeed:
    """
    Subscribes to the Polymarket CLOB WebSocket for orderbook/price_change
    events and exposes async-safe snapshot getters.
    """

    def __init__(self, config: BotConfig, market_ids: Iterable[str]) -> None:
        self._config = config
        self._market_ids: list[str] = list(market_ids)
        self._snapshots: dict[str, MarketSnapshot] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Run loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        if not self._market_ids:
            logger.warning("polymarket_feed_no_markets_configured")
            return

        backoff = 1.0
        while True:
            try:
                async with websockets.connect(
                    self._config.api.polymarket_ws_url,
                    ping_interval=None,   # disable library pings — we send app-level pings
                    close_timeout=5,
                ) as ws:
                    await self._subscribe(ws)
                    backoff = 1.0  # reset on successful connect
                    # Run message receiver and heartbeat concurrently
                    await asyncio.gather(
                        self._receive_loop(ws),
                        self._heartbeat_loop(ws),
                    )
            except ConnectionClosed as exc:
                logger.warning("polymarket_ws_closed", reason=str(exc), retry_in=backoff)
                from observability.metrics_exporter import get_metrics_exporter

                mx = get_metrics_exporter()
                if mx:
                    mx.record_feed_disconnect("polymarket_ws_closed")
            except Exception as exc:
                logger.exception("polymarket_feed_error", error=str(exc), retry_in=backoff)
                from observability.metrics_exporter import get_metrics_exporter

                mx = get_metrics_exporter()
                if mx:
                    mx.record_feed_disconnect("polymarket_feed_error")

            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _MAX_BACKOFF)

    async def _receive_loop(self, ws: Any) -> None:
        """Receive and process messages until connection closes."""
        async for raw in ws:
            await self._on_message(raw)

    async def _heartbeat_loop(self, ws: Any) -> None:
        """Send application-level ping every 10s to keep connection alive."""
        while True:
            await asyncio.sleep(10)
            try:
                await ws.send(json.dumps({"type": "ping"}))
                logger.debug("polymarket_ws_ping_sent")
            except Exception:
                break  # connection closed, let receive_loop handle reconnect

    async def _subscribe(self, ws: Any) -> None:
        # Exact subscription format required by Polymarket WS server
        sub = {
            "type": "market",
            "assets_ids": self._market_ids,
        }
        await ws.send(json.dumps(sub))
        logger.info("polymarket_ws_subscribed", market_count=len(self._market_ids))

    # ------------------------------------------------------------------
    # Message parsing
    # ------------------------------------------------------------------

    async def _on_message(self, raw: str | bytes) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return

        # Polymarket WS sends a list of market event objects
        events: list[dict[str, Any]] = data if isinstance(data, list) else [data]

        for payload in events:
            if not isinstance(payload, dict):
                continue

            # Real format: {"market": "0x...", "price_changes": [...]}
            # or          {"market": "0x...", "bids": [...], "asks": [...]}
            # or          {"event_type": "...", ...}  (legacy)

            if "price_changes" in payload:
                await self._handle_price_changes(payload)
            elif "bids" in payload or "asks" in payload:
                await self._handle_book_snapshot(payload)
            else:
                # Legacy fallback
                await self._process_event(payload)

    async def _handle_price_changes(self, payload: dict[str, Any]) -> None:
        """
        Handle real Polymarket WS price_changes format:
        {"market": "0x...", "price_changes": [
            {"asset_id": "123...", "price": "0.96", "size": "100",
             "side": "BUY", "best_bid": "0.974", "best_ask": "0.975"}
        ]}
        """
        changes = payload.get("price_changes") or []
        for change in changes:
            token_id = str(change.get("asset_id") or "")
            if not token_id:
                continue
            try:
                best_bid = float(change.get("best_bid") or 0.01)
                best_ask = float(change.get("best_ask") or 0.99)
                best_bid = max(0.01, min(0.99, best_bid))
                best_ask = max(0.01, min(0.99, best_ask))
                await self._upsert_snapshot(token_id, best_bid, best_ask, 0.0, 0.01)
            except (ValueError, TypeError):
                continue

    async def _handle_book_snapshot(self, payload: dict[str, Any]) -> None:
        """
        Handle full book snapshot:
        {"market": "0x...", "asset_id": "123...",
         "bids": [{"price": "0.45", "size": "100"}],
         "asks": [{"price": "0.46", "size": "200"}]}
        """
        token_id = str(payload.get("asset_id") or "")
        if not token_id:
            return
        bids = payload.get("bids") or []
        asks = payload.get("asks") or []
        try:
            best_bid = max(float(b["price"]) for b in bids if b.get("price")) if bids else 0.01
            best_ask = min(float(a["price"]) for a in asks if a.get("price")) if asks else 0.99
            best_bid = max(0.01, min(0.99, best_bid))
            best_ask = max(0.01, min(0.99, best_ask))
            await self._upsert_snapshot(token_id, best_bid, best_ask, 0.0, 0.01)
        except (ValueError, TypeError, ValueError):
            pass

    async def _process_event(self, payload: dict[str, Any]) -> None:
        """
        Handle Polymarket WS events. Two main formats:

        book event:
          {"event_type": "book", "asset_id": "123...",
           "bids": [{"price": "0.45", "size": "100"}, ...],
           "asks": [{"price": "0.46", "size": "200"}, ...]}

        price_change event:
          {"event_type": "price_change", "changes": [
            {"asset_id": "123...", "price": "0.45", "side": "BUY", "size": "50"}
          ]}

        last_trade_price event:
          {"event_type": "last_trade_price", "asset_id": "123...", "price": "0.45"}
        """
        event = payload.get("event_type") or payload.get("type") or ""

        if event == "book":
            await self._handle_book_event(payload)
        elif event == "price_change":
            await self._handle_price_change_event(payload)
        elif event == "last_trade_price":
            await self._handle_last_trade_price(payload)

    async def _handle_book_event(self, payload: dict[str, Any]) -> None:
        """Handle full orderbook snapshot."""
        token_id = str(payload.get("asset_id") or "")
        if not token_id:
            return

        bids = payload.get("bids") or []
        asks = payload.get("asks") or []

        # Best bid = highest bid price, best ask = lowest ask price
        try:
            best_bid = max(float(b["price"]) for b in bids if b.get("price")) if bids else 0.01
            best_ask = min(float(a["price"]) for a in asks if a.get("price")) if asks else 0.99
        except (ValueError, KeyError):
            return

        best_bid = max(0.01, min(0.99, best_bid))
        best_ask = max(0.01, min(0.99, best_ask))
        liq = float(payload.get("liquidity") or 0.0)
        tick = float(payload.get("tick_size") or 0.01)

        await self._upsert_snapshot(token_id, best_bid, best_ask, liq, tick)

    async def _handle_price_change_event(self, payload: dict[str, Any]) -> None:
        """Handle incremental price change — may have nested changes list."""
        # Format A: changes list
        changes = payload.get("changes") or []
        for change in changes:
            token_id = str(change.get("asset_id") or "")
            price = float(change.get("price") or 0)
            side = str(change.get("side") or "").upper()
            if not token_id or not price:
                continue
            async with self._lock:
                existing = self._snapshots.get(token_id)
                if existing:
                    if side == "BUY":
                        snap = existing.model_copy(update={"yes_bid": max(0.01, min(0.99, price))})
                    else:
                        snap = existing.model_copy(update={"yes_ask": max(0.01, min(0.99, price))})
                    self._snapshots[token_id] = snap
                    logger.debug("price_change_applied", token=token_id[:12], side=side, price=price)

        # Format B: flat fields on payload itself
        token_id = str(payload.get("asset_id") or "")
        price_str = payload.get("price") or payload.get("best_bid") or payload.get("best_ask")
        if token_id and price_str:
            try:
                price = float(price_str)
                bid = float(payload.get("best_bid") or price)
                ask = float(payload.get("best_ask") or price)
                await self._upsert_snapshot(token_id, bid, ask, 0.0, 0.01)
            except (ValueError, TypeError):
                pass

    async def _handle_last_trade_price(self, payload: dict[str, Any]) -> None:
        """Handle last trade price update."""
        token_id = str(payload.get("asset_id") or "")
        price_str = payload.get("price")
        if not token_id or not price_str:
            return
        try:
            price = max(0.01, min(0.99, float(price_str)))
            async with self._lock:
                existing = self._snapshots.get(token_id)
                if existing:
                    snap = existing.model_copy(update={
                        "yes_bid": max(0.01, price - 0.01),
                        "yes_ask": min(0.99, price + 0.01),
                    })
                    self._snapshots[token_id] = snap
                    logger.debug("last_trade_applied", token=token_id[:12], price=price)
        except (ValueError, TypeError):
            pass

    async def _upsert_snapshot(
        self,
        token_id: str,
        bid: float,
        ask: float,
        liq: float,
        tick: float,
    ) -> None:
        """Create or update a snapshot for a token."""
        no_bid = max(0.01, min(0.99, 1.0 - ask))
        no_ask = max(0.01, min(0.99, 1.0 - bid))
        async with self._lock:
            existing = self._snapshots.get(token_id)
            if existing:
                snap = existing.model_copy(update={
                    "yes_bid": bid,
                    "yes_ask": ask,
                    "no_bid": no_bid,
                    "no_ask": no_ask,
                    "liquidity_usd": liq if liq > 0 else existing.liquidity_usd,
                })
            else:
                snap = MarketSnapshot(
                    market_id=token_id,
                    yes_bid=bid,
                    yes_ask=ask,
                    no_bid=no_bid,
                    no_ask=no_ask,
                    liquidity_usd=liq,
                    tick_size=tick,
                    yes_token_id=token_id,
                    no_token_id="",
                )
            self._snapshots[token_id] = snap
        logger.debug("snapshot_upserted", token=token_id[:12], bid=bid, ask=ask)


    # ------------------------------------------------------------------
    # Snapshot access
    # ------------------------------------------------------------------

    async def get_snapshot(self, market_id: str) -> MarketSnapshot | None:
        async with self._lock:
            return self._snapshots.get(market_id)

    async def get_all_snapshots(self) -> dict[str, MarketSnapshot]:
        async with self._lock:
            return dict(self._snapshots)

    def add_market(self, market_id: str) -> None:
        """Dynamically add a market ID (takes effect on next reconnect)."""
        if market_id not in self._market_ids:
            self._market_ids.append(market_id)

    @staticmethod
    async def poll_market_resolutions(
        market_ids: list[str],
        *,
        gamma_api_url: str = "https://gamma-api.polymarket.com",
    ) -> list[ResolutionEvent]:
        """
        Query Polymarket Gamma REST for closed markets and winning outcomes.

        `market_ids` are condition IDs (same as RiskManager position keys / discovery).
        """
        if not market_ids:
            return []

        import aiohttp

        unique_ids = list(dict.fromkeys(market_ids))
        out: list[ResolutionEvent] = []
        params: list[tuple[str, str]] = [
            ("condition_ids", str(mid)) for mid in unique_ids
        ]

        url = f"{gamma_api_url.rstrip('/')}/markets"
        timeout = aiohttp.ClientTimeout(total=30)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, params=params) as resp:
                resp.raise_for_status()
                data = await resp.json()

        rows = data if isinstance(data, list) else data.get("data", []) if isinstance(data, dict) else []
        if not isinstance(rows, list):
            return []

        seen: set[str] = set()
        want = {str(x).strip().lower() for x in unique_ids}
        for m in rows:
            if not isinstance(m, dict):
                continue
            ev = _parse_gamma_market_if_resolved(m)
            if ev is None:
                continue
            key = ev.market_id.strip().lower()
            if key not in want or key in seen:
                continue
            seen.add(key)
            out.append(ev)

        return out


poll_market_resolutions = PolymarketFeed.poll_market_resolutions


# ---------------------------------------------------------------------------
# Market discovery
# ---------------------------------------------------------------------------

async def discover_liquid_markets(
    host: str,
    min_liquidity_usd: float = 5_000.0,
    tags: list[str] | None = None,
) -> list[dict[str, Any]]:
    """
    Fetch active crypto markets from the Polymarket Gamma API.

    Uses gamma-api.polymarket.com which supports proper active/closed
    filtering and volume sorting. Falls back to CLOB REST if Gamma fails.
    """
    import aiohttp

    keywords = [t.lower() for t in (tags or ["btc", "eth", "sol", "5-min", "15-min"])]
    results: list[dict[str, Any]] = []

    # Primary: Gamma API (correct active/closed filtering + volume sort)
    gamma_url = "https://gamma-api.polymarket.com/markets"
    params: dict[str, Any] = {
        "active": "true",
        "closed": "false",
        "limit": "100",
        "order": "volume24hr",
        "ascending": "false",
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                gamma_url, params=params, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()

        markets = data if isinstance(data, list) else data.get("data", [])
        logger.info("gamma_api_fetched", total=len(markets))

        for m in markets:
            question: str = str(m.get("question") or "").lower()
            if not any(kw in question for kw in keywords):
                continue
            if m.get("closed") or not m.get("active"):
                continue

            liq = float(m.get("liquidityNum") or m.get("liquidity") or 0.0)
            if liq < min_liquidity_usd:
                continue

            # Extract token IDs from clobTokenIds (may be a JSON string or list)
            raw_ids = m.get("clobTokenIds") or []
            if isinstance(raw_ids, str):
                try:
                    import json as _json
                    raw_ids = _json.loads(raw_ids)
                except Exception:
                    raw_ids = []
            tokens = m.get("tokens") or []
            if isinstance(raw_ids, list) and len(raw_ids) >= 2:
                yes_token, no_token = str(raw_ids[0]), str(raw_ids[1])
            elif len(tokens) >= 2:
                yes_token = str(tokens[0].get("token_id", ""))
                no_token = str(tokens[1].get("token_id", ""))
            else:
                yes_token, no_token = "", ""

            condition_id = str(m.get("conditionId") or m.get("condition_id") or "")
            if not condition_id:
                continue

            results.append({
                "market_id": condition_id,
                "yes_token_id": yes_token,
                "no_token_id": no_token,
                "question": m.get("question", ""),
                "liquidity_usd": liq,
                "volume_24hr": float(m.get("volume24hr") or 0.0),
            })

        logger.info("market_discovery_complete", found=len(results))
        return results

    except Exception as exc:
        logger.exception("gamma_api_failed", error=str(exc))

    # Fallback: CLOB REST API
    logger.warning("falling_back_to_clob_api")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{host}/markets",
                params={"limit": "100"},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
        markets = data.get("data", []) if isinstance(data, dict) else data
        for m in markets:
            if m.get("closed"):
                continue
            question = str(m.get("question") or "").lower()
            if not any(kw in question for kw in keywords):
                continue
            liq = float(m.get("liquidity") or 0.0)
            if liq < min_liquidity_usd:
                continue
            tokens = m.get("tokens", [])
            results.append({
                "market_id": str(m.get("condition_id") or ""),
                "yes_token_id": tokens[0].get("token_id", "") if len(tokens) > 0 else "",
                "no_token_id": tokens[1].get("token_id", "") if len(tokens) > 1 else "",
                "question": m.get("question", ""),
                "liquidity_usd": liq,
                "volume_24hr": 0.0,
            })
    except Exception as exc2:
        logger.exception("clob_fallback_failed", error=str(exc2))

    logger.info("market_discovery_fallback_complete", found=len(results))
    return results
