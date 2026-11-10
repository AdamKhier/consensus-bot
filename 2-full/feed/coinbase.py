"""Coinbase Advanced Trade adapter - a second opinion, not a source of truth.

Chainlink's Data Streams aggregate across venues, so a single-exchange feed
carries some proxy error against whatever actually resolves the market. Coinbase
runs alongside Binance purely to *measure* that error: we log the cross-venue
basis on every tick, and if it stays tight, Binance-only is validated and this
can be switched off without changing any downstream logic.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Iterator

from . import ws
from .types import Side, Tick

WS_URL = "wss://advanced-trade-ws.coinbase.com"
PRODUCT_ID = "BTC-USD"


def stream_url(product: str = PRODUCT_ID) -> str:
    return f"{WS_URL}?product_ids={product}"


def subscribe(connection) -> None:
    """Send the market_trades and ticker subscriptions.

    The server is silent until it receives this, and a missed subscribe after a
    reconnect leaves you connected to nothing.
    """
    for channel in ("market_trades", "ticker"):
        connection.send_text(
            json.dumps(
                {
                    "type": "subscribe",
                    "product_ids": [PRODUCT_ID],
                    "channel": channel,
                }
            )
        )


def parse_time(value: str) -> int:
    """Parse Coinbase's ISO-8601 timestamps to epoch milliseconds."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return int(time.time() * 1000)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def parse(message: str, product: str = PRODUCT_ID) -> tuple[Tick | None, dict]:
    """Parse a Coinbase frame into our normalised shapes."""
    now_ms = int(time.time() * 1000)
    envelope = ws.parse_json(message)
    if not envelope:
        return None, {}

    for event in envelope.get("events") or []:
        for trade in event.get("trades") or []:
            try:
                price = float(trade["price"])
                qty = float(trade["size"])
                event_ms = parse_time(trade["time"])
            except (KeyError, ValueError, TypeError):
                continue
            side = Side.SELL if str(trade.get("side", "")).upper() == "SELL" else Side.BUY
            return Tick("coinbase", product, price, qty, side, event_ms, now_ms), {}

        for ticker in event.get("tickers") or []:
            try:
                return None, {
                    "best_bid": float(ticker["best_bid"]),
                    "best_ask": float(ticker["best_ask"]),
                    "event_ms": now_ms,
                }
            except (KeyError, ValueError, TypeError):
                continue

    return None, {}


def run(
    product: str = PRODUCT_ID,
    *,
    reconnect: bool = True,
    stats: ws.Stats | None = None,
) -> Iterator[tuple[Tick | None, dict, ws.Stats]]:
    """Yield (tick, quote, stats) from the live Coinbase feed."""
    live = stats if stats is not None else ws.Stats()
    for message in ws.stream(
        stream_url(product),
        reconnect=reconnect,
        stats=live,
        on_connect=subscribe,
    ):
        tick, quote = parse(message, product)
        if tick is None and not quote:
            continue
        yield tick, quote, live
