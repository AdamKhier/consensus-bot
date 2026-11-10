"""Binance venue adapter.

Binance is our source of truth for labelled outcomes. `aggTrade` gives us every
consolidated fill (best for reconstructing the true trade path); `bookTicker`
gives us a top-of-book quote that updates faster than trades, so we see
liquidity appearing and vanishing even when nothing has printed yet.
"""

from __future__ import annotations

import time
from typing import Iterator

from . import ws
from .types import Side, Tick, VenueState

SPOT_HOST = "stream.binance.com:9443"
TIME_URL = "https://api.binance.com/api/v3/time"
DEPTH_URL = "https://api.binance.com/api/v3/depth"


def stream_url(symbol: str = "btcusdt") -> str:
    streams = "/".join(
        [
            f"{symbol}@aggTrade",
            f"{symbol}@bookTicker",
            f"{symbol}@depth5@100ms",
        ]
    )
    return f"wss://{SPOT_HOST}/stream?streams={streams}"


def parse(message: str, symbol: str = "BTCUSDT") -> tuple[Tick | None, dict]:
    """Parse one combined-stream frame.

    Returns (tick, quote) where tick is set for trades and quote carries
    best bid/ask for bookTicker and depth frames.
    """
    now_ms = int(time.time() * 1000)
    envelope = ws.parse_json(message)
    if not envelope:
        return None, {}

    stream_name = envelope.get("stream", "")
    data = envelope.get("data", envelope)
    event_type = data.get("e", "")

    if event_type == "aggTrade":
        try:
            price = float(data["p"])
            qty = float(data["q"])
            event_ms = int(data["T"])
        except (KeyError, ValueError, TypeError):
            return None, {}
        # `m` is "is buyer the market maker", so a True means the aggressor sold.
        side = Side.SELL if data.get("m") else Side.BUY
        return Tick("binance", symbol, price, qty, side, event_ms, now_ms), {}

    if event_type == "bookTicker" or ("b" in data and "a" in data and "e" not in data):
        try:
            quote = {
                "best_bid": float(data["b"]),
                "best_ask": float(data["a"]),
                "event_ms": int(data.get("E") or data.get("T") or now_ms),
            }
        except (ValueError, TypeError, KeyError):
            return None, {}
        return None, quote

    if "bids" in data and "asks" in data:
        try:
            quote = {
                "best_bid": float(data["bids"][0][0]),
                "best_ask": float(data["asks"][0][0]),
                "event_ms": int(data.get("E") or now_ms),
            }
        except (ValueError, TypeError, IndexError, KeyError):
            return None, {}
        return None, quote

    return None, {}


def run(
    symbol: str = "btcusdt",
    *,
    reconnect: bool = True,
    stats: ws.Stats | None = None,
) -> Iterator[tuple[Tick | None, dict, ws.Stats]]:
    """Yield (tick, quote, stats) tuples from the live Binance stream."""
    live = stats if stats is not None else ws.Stats()
    for message in ws.stream(stream_url(symbol), reconnect=reconnect, stats=live):
        tick, quote = parse(message, symbol.upper())
        if tick is None and not quote:
            continue
        yield tick, quote, live
