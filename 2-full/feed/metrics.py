"""Rolling price statistics.

The important one is the time-weighted average. Polymarket's newer 5-minute
markets resolve against Chainlink's BTC/USD *TWAP 60s* stream, so the number
that decides Up vs Down is a 60-second time-weighted average, not a spot print.
Taking a plain mean of the last N ticks would be wrong twice over: it would
weight a burst of tiny trades equally with a single large one, and it would
drift whenever ticks arrive irregularly.

A true TWAP integrates price over time. Between consecutive observations the
price is assumed constant at the earlier value, so the area under the curve is
sum(price_i * dt_i) and dividing by total elapsed time gives the average.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field


@dataclass
class TwapResult:
    value: float | None
    window_s: float
    samples: int
    coverage: float

    @property
    def valid(self) -> bool:
        return self.value is not None and self.coverage >= 0.9


class TimeWeightedAverage:
    """Time-weighted rolling average over a trailing time window."""

    def __init__(self, window_s: float = 60.0) -> None:
        self.window_s = window_s
        self._points: deque[tuple[float, float]] = deque()
        self.last: float | None = None

    def add(self, price: float, ts: float | None = None) -> None:
        """Record an observation. `ts` is epoch seconds."""
        if price <= 0:
            return
        if ts is None:
            ts = time.time()
        self.last = price
        if self._points and ts < self._points[-1][0]:
            # Out-of-order observation: keep the series monotonic so dt stays
            # non-negative, otherwise the integral is corrupted.
            ts = self._points[-1][0]
        self._points.append((ts, price))
        self._evict(ts)

    def _evict(self, now: float) -> None:
        cutoff = now - self.window_s
        while len(self._points) > 1 and self._points[0][0] < cutoff:
            self._points.popleft()

    def result(self, now: float | None = None) -> TwapResult:
        """TWAP over the trailing window, plus how much of it we actually saw."""
        if now is None:
            now = time.time()
        self._evict(now)

        if len(self._points) < 2:
            price = self._points[-1][1] if self._points else None
            return TwapResult(price, 0.0, len(self._points), 0.0)

        newest_ts = self._points[-1][0]
        # Extend the final observation to `now` so a quiet tail still counts.
        points = list(self._points) + [(now, self._points[-1][1])]
        if points[-1][0] <= points[0][0]:
            return TwapResult(self._points[-1][1], 0.0, len(self._points), 0.0)

        area = 0.0
        total = 0.0
        for (t0, p0), (t1, _) in zip(points, points[1:]):
            dt = t1 - t0
            if dt <= 0:
                continue
            area += p0 * dt
            total += dt

        if total <= 0:
            return TwapResult(self._points[-1][1], 0.0, len(self._points), 0.0)

        # How much of the window the data actually spans, so a startup that has
        # only been running 5s is not reported as a confident 60s TWAP.
        coverage = min(1.0, total / self.window_s)
        return TwapResult(area / total, total, len(self._points), coverage)


class RollingMean:
    """Plain mean of the last N observations, kept for comparison against TWAP."""

    def __init__(self, size: int = 100) -> None:
        self._values: deque[float] = deque(maxlen=size)
        self.last: float | None = None

    def add(self, price: float) -> None:
        if price <= 0:
            return
        self._values.append(price)
        self.last = price

    def value(self) -> float | None:
        if not self._values:
            return None
        return sum(self._values) / len(self._values)

    def __len__(self) -> int:
        return len(self._values)


@dataclass
class PriceHistory:
    """Recent price path, for the sparkline and for round high/low tracking."""

    points: deque[tuple[float, float]] = field(default_factory=lambda: deque(maxlen=3600))

    def add(self, price: float, ts: float | None = None) -> None:
        if price <= 0:
            return
        if ts is None:
            ts = time.time()
        self.points.append((ts, price))

    def high_low(self, start_ts: float, end_ts: float) -> tuple[float | None, float | None]:
        prices = [p for t, p in self.points if start_ts <= t < end_ts]
        if not prices:
            return None, None
        return min(prices), max(prices)

    def last_n(self, seconds: float, now: float | None = None) -> list[float]:
        if now is None:
            now = time.time()
        return [p for t, p in self.points if t > now - seconds]


class RollingWindow:
    """Tracks high/low/vwap within the current 5-minute window."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.high: float | None = None
        self.low: float | None = None
        self.vwap_num: float = 0.0
        self.vwap_den: float = 0.0
        self._last_price: float | None = None
        self._last_ts: float | None = None

    def add(self, price: float, ts: float | None = None) -> None:
        if price <= 0:
            return
        if ts is None:
            ts = time.time()
        self.high = price if self.high is None else max(self.high, price)
        self.low = price if self.low is None else min(self.low, price)
        if self._last_price is not None and self._last_ts is not None:
            dt = ts - self._last_ts
            if dt > 0:
                self.vwap_num += self._last_price * dt
                self.vwap_den += dt
        self._last_price = price
        self._last_ts = ts

    @property
    def vwap(self) -> float | None:
        if self.vwap_den <= 0:
            return None
        return self.vwap_num / self.vwap_den
