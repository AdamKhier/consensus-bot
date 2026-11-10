"""5-minute window state machine.

Polymarket's BTC 5-minute markets are identified by
`btc-updown-5m-<start_epoch>` where `start_epoch % 300 == 0`, and resolve "Up" if
the closing price is greater than or equal to the opening price. Note the tie
rule: equal prices resolve Up, not a coin flip.

Freezing the open price correctly is the entire game. Sample it one second late
or use a stale quote and every label we produce is wrong, and the strategies
trained on those labels inherit the error silently. So the open is captured from
the exchange-clock-corrected timestamp at the boundary, and the same second
yields both a spot open and a TWAP open, because which one decides the market
depends on which resolution variant is live.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .metrics import RollingWindow

WINDOW_SECONDS = 300

# A window is suspect if we saw almost no trades, or if the feed went quiet for
# a stretch well beyond what a merely calm market would produce. Both point at
# missed data rather than a genuinely flat window.
MIN_TICKS = 20
MAX_TICK_GAP_S = 5.0

# If our first tick for a window lands more than this far into the window, we
# never observed the real open. The recorded "open" is just the first price we
# happened to see, which silently produces a wrong label.
MAX_OPEN_DELAY_S = 5.0


def window_start(ts: float) -> int:
    """Epoch second at which the window containing `ts` began."""
    return int(ts) // WINDOW_SECONDS * WINDOW_SECONDS


def window_end(start: int) -> int:
    return start + WINDOW_SECONDS


def market_slug(start: int) -> str:
    return f"btc-updown-5m-{start}"


@dataclass
class WindowResult:
    """A completed 5-minute round, i.e. one labelled training example."""

    start: int
    end: int
    open_spot: float | None
    close_spot: float | None
    open_twap: float | None
    close_twap: float | None
    high: float | None
    low: float | None
    vwap: float | None
    tick_count: int
    outcome: str | None
    gap_detected: bool = False
    max_tick_gap_s: float = 0.0
    open_delay_s: float = 0.0
    late_open: bool = False
    partial: bool = False
    venue: str = "binance"

    @property
    def usable_for_training(self) -> bool:
        """Rounds that saw the whole window and a healthy tick rate."""
        return not (self.partial or self.gap_detected or self.late_open)

    @property
    def change_bps(self) -> float | None:
        if not self.open_spot or not self.close_spot:
            return None
        return (self.close_spot - self.open_spot) / self.open_spot * 10_000

    def to_dict(self) -> dict:
        return {
            "type": "round",
            "venue": self.venue,
            "slug": market_slug(self.start),
            "start": self.start,
            "end": self.end,
            "open_spot": self.open_spot,
            "close_spot": self.close_spot,
            "open_twap": self.open_twap,
            "close_twap": self.close_twap,
            "high": self.high,
            "low": self.low,
            "vwap": self.vwap,
            "tick_count": self.tick_count,
            "max_tick_gap_s": self.max_tick_gap_s,
            "open_delay_s": self.open_delay_s,
            "late_open": self.late_open,
            "outcome": self.outcome,
            "change_bps": self.change_bps,
            "gap_detected": self.gap_detected,
            "partial": self.partial,
            "usable_for_training": self.usable_for_training,
        }


@dataclass
class FiveMinuteTracker:
    """Tracks the live window and emits a result each time one closes.

    `twap_fn` is a callable returning the current trailing 60s TWAP, injected
    rather than imported so the tracker stays testable without a live feed.
    """

    twap_fn: object = None
    venue: str = "binance"
    start: int = 0
    open_spot: float | None = None
    close_spot: float | None = None
    open_twap: float | None = None
    close_twap: float | None = None
    tick_count: int = 0
    max_tick_gap_s: float = 0.0
    open_delay_s: float = 0.0
    late_open: bool = False
    last_price: float | None = None
    _rolls: RollingWindow = field(default_factory=RollingWindow)
    _last_tick_ts: float | None = None

    def __post_init__(self) -> None:
        if self.start == 0:
            self.start = window_start(time.time())

    # -- live state -------------------------------------------------------

    @property
    def end(self) -> int:
        return window_end(self.start)

    def seconds_remaining(self, now: float | None = None) -> float:
        if now is None:
            now = time.time()
        return max(0.0, self.end - now)

    def elapsed(self, now: float | None = None) -> float:
        if now is None:
            now = time.time()
        return max(0.0, now - self.start)

    def progress(self, now: float | None = None) -> float:
        return min(1.0, self.elapsed(now) / WINDOW_SECONDS)

    @property
    def high(self) -> float | None:
        return self._rolls.high

    @property
    def low(self) -> float | None:
        return self._rolls.low

    @property
    def vwap(self) -> float | None:
        return self._rolls.vwap

    @property
    def current_twap(self) -> float | None:
        if self.twap_fn is None:
            return None
        try:
            return self.twap_fn()
        except Exception:
            return None

    def change_from_open(self, price: float | None = None) -> float | None:
        """Signed change vs the window open, in basis points."""
        p = price if price is not None else self.last_price
        if not p or not self.open_spot:
            return None
        return (p - self.open_spot) / self.open_spot * 10_000

    def live_outcome(self, price: float | None = None) -> str | None:
        """Which way the market would resolve right now."""
        p = price if price is not None else self.last_price
        if p is None or self.open_spot is None:
            return None
        return "up" if p >= self.open_spot else "down"

    # -- ingestion --------------------------------------------------------

    def add_tick(self, price: float, ts: float) -> WindowResult | None:
        """Feed a price. Returns a completed WindowResult if this crossed a boundary."""
        completed: WindowResult | None = None
        start = window_start(ts)

        if start != self.start:
            completed = self._roll(ts)
            # A long stall can skip whole windows; roll forward without inventing
            # data for the windows we slept through.
            while self.start < start:
                self.start = window_end(self.start)

        if self.open_spot is None:
            self.open_spot = price
            self.open_twap = self.current_twap
            self._last_tick_ts = ts
            # How far into the window we started. A late start means the open we
            # record is not the market's real open.
            self.open_delay_s = max(0.0, ts - self.start)
            self.late_open = self.open_delay_s > MAX_OPEN_DELAY_S

        if self._last_tick_ts is not None:
            self.max_tick_gap_s = max(self.max_tick_gap_s, ts - self._last_tick_ts)
        self._last_tick_ts = ts

        self.last_price = price
        self.tick_count += 1
        self._rolls.add(price, ts)
        return completed

    def flush(self, now: float | None = None) -> WindowResult | None:
        """Close the current window early, e.g. on shutdown.

        The result is marked partial: the close price is the last trade we saw,
        not the last trade of the window, so it must not be used as a label.
        """
        if now is None:
            now = time.time()
        if self.open_spot is None:
            return None
        return self._roll(now, partial=True)

    # -- boundary handling ------------------------------------------------

    def _roll(self, now: float, partial: bool = False) -> WindowResult | None:
        if self.open_spot is None:
            return None

        self.close_spot = self.last_price
        self.close_twap = self.current_twap
        result = WindowResult(
            start=self.start,
            end=self.end,
            open_spot=self.open_spot,
            close_spot=self.close_spot,
            open_twap=self.open_twap,
            close_twap=self.close_twap,
            high=self._rolls.high,
            low=self._rolls.low,
            vwap=self._rolls.vwap,
            tick_count=self.tick_count,
            outcome=self._resolve(),
            gap_detected=self._gap_detected(),
            max_tick_gap_s=self.max_tick_gap_s,
            open_delay_s=self.open_delay_s,
            late_open=self.late_open,
            partial=partial,
            venue=self.venue,
        )

        self.open_spot = None
        self.close_spot = None
        self.open_twap = None
        self.close_twap = None
        self.tick_count = 0
        self.max_tick_gap_s = 0.0
        self.open_delay_s = 0.0
        self.late_open = False
        self._last_tick_ts = None
        self._rolls = RollingWindow()
        return result

    def _resolve(self) -> str | None:
        # Polymarket resolves Up when close >= open. Ties are Up.
        if self.close_spot is None or self.open_spot is None:
            return None
        return "up" if self.close_spot >= self.open_spot else "down"

    def _gap_detected(self) -> bool:
        """Flag windows with implausible tick coverage, i.e. probable feed loss."""
        if self.tick_count < MIN_TICKS:
            return True
        return self.max_tick_gap_s > MAX_TICK_GAP_S
