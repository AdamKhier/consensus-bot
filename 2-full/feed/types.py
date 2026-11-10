"""Normalised market-data types shared by every venue adapter."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True)
class Tick:
    """A single normalised price observation.

    `event_ms` is the venue's own timestamp for the trade; `recv_ms` is our
    local receive time. The gap between them is feed latency, which we track
    because it is the difference between seeing a move and being able to trade it.
    """

    venue: str
    symbol: str
    price: float
    qty: float
    side: Side
    event_ms: int
    recv_ms: int

    @property
    def latency_ms(self) -> float:
        return self.recv_ms - self.event_ms

    def to_dict(self) -> dict:
        return {
            "venue": self.venue,
            "symbol": self.symbol,
            "price": self.price,
            "qty": self.qty,
            "side": self.side.value,
            "event_ms": self.event_ms,
            "recv_ms": self.recv_ms,
        }


@dataclass
class VenueState:
    """Latest known state for one venue, used for cross-venue divergence."""

    venue: str
    last_price: float = 0.0
    last_event_ms: int = 0
    last_recv_ms: int = 0
    best_bid: float = 0.0
    best_ask: float = 0.0
    ticks: int = 0

    @property
    def mid(self) -> float:
        if self.best_bid and self.best_ask:
            return (self.best_bid + self.best_ask) / 2
        return self.last_price
