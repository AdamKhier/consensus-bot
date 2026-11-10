"""Feed engine: joins the venue streams, window tracker, metrics and recorder.

This is the single place that knows how the pieces fit together, so the terminal
UI and the headless daemon share identical behaviour and any divergence between
what you watch and what gets recorded is impossible by construction.
"""

from __future__ import annotations

import os
import queue
import threading
import time
from dataclasses import dataclass, field

from . import binance, clock as clock_mod, coinbase, recorder as recorder_mod
from . import ws
from .metrics import PriceHistory, RollingMean, RollingWindow, TimeWeightedAverage
from .types import Tick, VenueState
from .windows import FiveMinuteTracker, WindowResult, window_end, window_start


@dataclass
class EngineState:
    """Everything the UI needs to render one frame."""

    last_price: float | None = None
    last_event_ms: int = 0
    latency_ms: float | None = None
    best_bid: float = 0.0
    best_ask: float = 0.0
    spread_bps: float | None = None
    open_spot: float | None = None
    change_bps: float | None = None
    live_outcome: str | None = None
    twap_60s: float | None = None
    twap_coverage: float = 0.0
    mean_100: float | None = None
    high: float | None = None
    low: float | None = None
    vwap: float | None = None
    seconds_remaining: float = 0.0
    window_progress: float = 0.0
    window_start: int = 0
    tick_count: int = 0
    msg_rate: float = 0.0
    binance: VenueState = field(default_factory=lambda: VenueState("binance"))
    coinbase: VenueState = field(default_factory=lambda: VenueState("coinbase"))
    basis_bps: float | None = None
    rounds: int = 0
    clock_offset_ms: float = 0.0
    ws_stats: ws.Stats = field(default_factory=ws.Stats)
    connected: bool = False


class Engine:
    """Owns all feed state. Thread-safe: feed threads push, the UI thread reads."""

    def __init__(
        self,
        *,
        symbol: str = "btcusdt",
        data_dir: str = "data",
        record_ticks: bool = True,
        use_coinbase: bool = True,
        twap_window_s: float = 60.0,
    ) -> None:
        self.symbol = symbol
        self.use_coinbase = use_coinbase
        self.clock = clock_mod.ClockOffset()
        self.recorder = recorder_mod.Recorder(data_dir, record_ticks=record_ticks)
        self.twap = TimeWeightedAverage(twap_window_s)
        self.mean = RollingMean(100)
        self.history = PriceHistory()
        self.tracker = FiveMinuteTracker(twap_fn=lambda: self.twap.value)
        self.state = EngineState(window_start=self.tracker.start)
        self._lock = threading.RLock()
        self._rounds: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    # -- lifecycle --------------------------------------------------------

    def start(self, wait_for_boundary: bool = False) -> None:
        self.clock = clock_mod.sync_offset(binance.TIME_URL, samples=5)
        self.state.clock_offset_ms = self.clock.offset_ms
        # Re-anchor the window to exchange time now that the offset is known.
        with self._lock:
            self.tracker.start = window_start(self.clock.now())
            self.state.window_start = self.tracker.start
            self.state.seconds_remaining = self.tracker.seconds_remaining(self.clock.now())

        if wait_for_boundary:
            self._wait_for_boundary()
        self.recorder.write_event(
            "startup",
            symbol=self.symbol,
            clock_offset_ms=round(self.clock.offset_ms, 1),
            rtt_ms=round(self.clock.rtt_ms, 1),
            use_coinbase=self.use_coinbase,
            pid=os.getpid(),
        )
        for target, name in ((self._binance_loop, "binance"), (self._coinbase_loop, "coinbase")):
            if name == "coinbase" and not self.use_coinbase:
                continue
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)

    def _wait_for_boundary(self) -> None:
        """Idle until the next 5-minute window opens.

        Starting mid-window guarantees the first round's "open" is wrong, since
        we never saw the real one. Waiting costs at most five idle minutes and
        buys a clean first label.

        The target boundary is computed once and slept against directly. Polling
        "am I near the end of this window?" instead is a race: the sleep steps
        over the boundary, the next poll sees a brand new window, and the wait
        silently extends by another full five minutes.
        """
        target = window_end(window_start(self.clock.now()))
        while not self._stop.is_set():
            remaining = target - self.clock.now()
            if remaining <= 0:
                break
            time.sleep(min(0.5, remaining))

        # Anchor exactly on the boundary, not on whenever the poll happened to
        # wake up, so the first tick's open_delay is genuinely ~0. Read the clock
        # once and reuse it so all three values describe the same instant.
        now = self.clock.now()
        with self._lock:
            self.tracker.start = max(target, window_start(now))
            self.state.window_start = self.tracker.start
            self.state.seconds_remaining = self.tracker.seconds_remaining(now)

    def stop(self) -> None:
        self._stop.set()

    def close(self, join_timeout: float = 5.0) -> None:
        self.stop()
        # Wait for the feed threads to actually exit before flushing, otherwise a
        # tick can land mid-flush and produce a bogus one-tick window.
        for thread in self._threads:
            thread.join(timeout=join_timeout)
        result = self.tracker.flush(self.clock.now())
        if result is not None:
            self.recorder.write_round(result)
        self.recorder.write_event("shutdown", rounds=self.state.rounds)
        self.recorder.close()

    def __enter__(self) -> "Engine":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def pop_round(self) -> WindowResult | None:
        try:
            return self._rounds.get_nowait()
        except queue.Empty:
            return None

    # -- feed loops -------------------------------------------------------

    def _binance_loop(self) -> None:
        for tick, quote, stats in binance.run(self.symbol, reconnect=True):
            if self._stop.is_set():
                return
            now = self.clock.now()
            if tick is not None:
                self._on_binance_tick(tick, now)
            if quote:
                self._on_binance_quote(quote, now)
            self._update_rate(stats, now)

    def _coinbase_loop(self) -> None:
        for tick, quote, _stats in coinbase.run(reconnect=True):
            if self._stop.is_set():
                return
            with self._lock:
                if tick is not None:
                    self.state.coinbase.last_price = tick.price
                    self.state.coinbase.last_event_ms = tick.event_ms
                    self.state.coinbase.last_recv_ms = tick.recv_ms
                    self.state.coinbase.ticks += 1
                if quote:
                    self.state.coinbase.best_bid = quote["best_bid"]
                    self.state.coinbase.best_ask = quote["best_ask"]
                self._recompute_basis()

    def _on_binance_tick(self, tick: Tick, now: float) -> None:
        self.twap.add(tick.price, now)
        self.mean.add(tick.price)
        self.history.add(tick.price, now)
        self.recorder.write_tick(tick)

        completed = self.tracker.add_tick(tick.price, now)
        if completed is not None:
            self.recorder.write_round(completed)
            self._rounds.put(completed)
            self._on_roll()

        with self._lock:
            state = self.state
            state.last_price = tick.price
            state.last_event_ms = tick.event_ms
            state.latency_ms = tick.latency_ms
            state.binance.last_price = tick.price
            state.binance.last_event_ms = tick.event_ms
            state.binance.last_recv_ms = tick.recv_ms
            state.binance.ticks += 1
            state.open_spot = self.tracker.open_spot
            state.change_bps = self.tracker.change_from_open(tick.price)
            state.live_outcome = self.tracker.live_outcome(tick.price)
            result = self.twap.result(now)
            state.twap_60s = result.value
            state.twap_coverage = result.coverage
            state.mean_100 = self.mean.value()
            state.high = self.tracker.high
            state.low = self.tracker.low
            state.vwap = self.tracker.vwap
            state.seconds_remaining = self.tracker.seconds_remaining(now)
            state.window_progress = self.tracker.progress(now)
            state.window_start = self.tracker.start
            state.tick_count = self.tracker.tick_count
            self._recompute_basis()

    def _on_roll(self) -> None:
        with self._lock:
            self.state.rounds += 1

    def _on_binance_quote(self, quote: dict, now: float) -> None:
        with self._lock:
            state = self.state
            state.best_bid = quote["best_bid"]
            state.best_ask = quote["best_ask"]
            if state.best_bid and state.best_ask:
                mid = (state.best_bid + state.best_ask) / 2
                state.spread_bps = (state.best_ask - state.best_bid) / mid * 10_000
            state.binance.best_bid = quote["best_bid"]
            state.binance.best_ask = quote["best_ask"]

    def _recompute_basis(self) -> None:
        state = self.state
        b, c = state.binance, state.coinbase
        if b.last_price and c.last_price:
            state.basis_bps = (c.last_price - b.last_price) / b.last_price * 10_000
        else:
            state.basis_bps = None

    def _update_rate(self, stats: ws.Stats, now: float) -> None:
        with self._lock:
            self.state.ws_stats = stats
            local_now = time.time()
            age = local_now - stats.connected_at if stats.connected_at else float("inf")
            self.state.connected = age < 30
            # Rate is per-connection, so it stays meaningful across reconnects.
            self.state.msg_rate = stats.messages / max(1e-6, age)

    # -- helpers ----------------------------------------------------------

    def sparkline(self, seconds: float = 300, width: int = 60) -> str:
        points = self.history.last_n(seconds, self.clock.now())
        return render_sparkline(points, width)

    def recent_rounds(self, count: int = 5) -> list:
        out = []
        try:
            with open(self.recorder.rounds_path) as handle:
                for line in handle:
                    line = line.strip()
                    if line:
                        out.append(line)
        except OSError:
            return []
        return out[-count:]


def render_sparkline(values: list[float], width: int = 60) -> str:
    """Render a price path as a unicode sparkline."""
    if len(values) < 2:
        return ""
    if len(values) > width:
        bucket = len(values) / width
        values = [values[int(i * bucket)] for i in range(width)]
    low, high = min(values), max(values)
    if high == low:
        return "─" * len(values)
    levels = "▁▂▃▄▅▆▇█"
    span = high - low
    return "".join(levels[min(len(levels) - 1, int((v - low) / span * (len(levels) - 1)))] for v in values)
