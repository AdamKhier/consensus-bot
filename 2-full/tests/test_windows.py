"""5-minute window boundary and resolution tests."""

from __future__ import annotations

import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import feed.engine as engine_mod  # noqa: E402
from feed.windows import (  # noqa: E402
    WINDOW_SECONDS,
    FiveMinuteTracker,
    market_slug,
    window_end,
    window_start,
)

# A known boundary: 2026-09-27 01:35:00 EDT == 1790487300
KNOWN_START = 1790487300


class TestWindowMath(unittest.TestCase):
    def test_start_is_epoch_aligned(self):
        self.assertEqual(window_start(KNOWN_START), KNOWN_START)
        self.assertEqual(KNOWN_START % WINDOW_SECONDS, 0)

    def test_mid_window_maps_back(self):
        self.assertEqual(window_start(KNOWN_START + 1), KNOWN_START)
        self.assertEqual(window_start(KNOWN_START + 299), KNOWN_START)
        self.assertEqual(window_start(KNOWN_START + 300), KNOWN_START + 300)

    def test_end_is_start_plus_300(self):
        self.assertEqual(window_end(KNOWN_START), KNOWN_START + 300)

    def test_slug_format(self):
        self.assertEqual(market_slug(KNOWN_START), f"btc-updown-5m-{KNOWN_START}")

    def test_arbitrary_timestamps_align(self):
        for ts in (0, 1, 299, 300, 1790487323, 1_700_000_000):
            self.assertEqual(window_start(ts) % WINDOW_SECONDS, 0)
            self.assertLessEqual(window_start(ts), ts)


class FakeTwap:
    def __init__(self) -> None:
        self.value = None

    def __call__(self):
        return self.value


class FakeClock:
    """Clock whose readings advance by a fixed step, for exercising poll loops."""

    def __init__(self, t: float, step: float = 0.5, freeze_first: bool = False) -> None:
        self.t = t
        self.step = step
        self.freeze_first = freeze_first
        self.steps = 0
        self._frozen = freeze_first

    def now(self) -> float:
        if self._frozen:
            self._frozen = False
            return self.t
        self.steps += 1
        self.t += self.step
        return self.t

    def now_ms(self) -> int:
        return int(self.now() * 1000)

    def advance_to(self, t: float) -> None:
        self.t = t
        self._frozen = False


def make_bare_engine(clock, start: int) -> engine_mod.Engine:
    """An Engine with only the fields _wait_for_boundary touches.

    `start` is passed in rather than read from the clock, so the fake clock's
    first reading is left for the code under test to consume.
    """
    engine = engine_mod.Engine.__new__(engine_mod.Engine)
    engine.clock = clock
    engine.tracker = FiveMinuteTracker(start=start)
    engine.state = engine_mod.EngineState(window_start=start)
    engine._stop = threading.Event()
    engine._lock = threading.RLock()
    return engine


class TestTrackerBasics(unittest.TestCase):
    def setUp(self):
        self.twap = FakeTwap()
        self.tracker = FiveMinuteTracker(twap_fn=self.twap, start=KNOWN_START)

    def test_first_tick_sets_open(self):
        self.tracker.add_tick(100.0, KNOWN_START + 0.5)
        self.assertEqual(self.tracker.open_spot, 100.0)
        self.assertEqual(self.tracker.last_price, 100.0)
        self.assertEqual(self.tracker.tick_count, 1)

    def test_no_result_before_boundary(self):
        for i in range(10):
            self.assertIsNone(self.tracker.add_tick(100.0 + i, KNOWN_START + i * 10))
        # The open must stay pinned to the first tick, not drift with price.
        self.assertEqual(self.tracker.open_spot, 100.0)
        self.assertEqual(self.tracker.tick_count, 10)

    def test_change_from_open_bps(self):
        self.tracker.add_tick(100.0, KNOWN_START)
        self.tracker.add_tick(101.0, KNOWN_START + 1)
        self.assertAlmostEqual(self.tracker.change_from_open(), 100.0, places=6)

    def test_negative_change(self):
        self.tracker.add_tick(100.0, KNOWN_START)
        self.tracker.add_tick(99.0, KNOWN_START + 1)
        self.assertAlmostEqual(self.tracker.change_from_open(), -100.0, places=6)

    def test_change_none_without_open(self):
        self.assertIsNone(self.tracker.change_from_open(100.0))

    def test_live_outcome_up_and_down(self):
        self.tracker.add_tick(100.0, KNOWN_START)
        self.assertEqual(self.tracker.live_outcome(101.0), "up")
        self.assertEqual(self.tracker.live_outcome(99.0), "down")

    def test_live_outcome_tie_is_up(self):
        self.tracker.add_tick(100.0, KNOWN_START)
        self.assertEqual(self.tracker.live_outcome(100.0), "up")

    def test_twap_open_captured_at_first_tick(self):
        self.twap.value = 99.5
        self.tracker.add_tick(100.0, KNOWN_START)
        self.assertEqual(self.tracker.open_twap, 99.5)

    def test_twap_exception_is_swallowed(self):
        def boom():
            raise RuntimeError("no data yet")

        tracker = FiveMinuteTracker(twap_fn=boom, start=KNOWN_START)
        tracker.add_tick(100.0, KNOWN_START)
        self.assertIsNone(tracker.current_twap)

    def test_seconds_remaining_and_progress(self):
        self.tracker.add_tick(100.0, KNOWN_START)
        self.assertAlmostEqual(self.tracker.seconds_remaining(KNOWN_START + 150), 150.0)
        self.assertAlmostEqual(self.tracker.progress(KNOWN_START + 150), 0.5, places=6)
        self.assertAlmostEqual(self.tracker.progress(KNOWN_START + 400), 1.0, places=6)

    def test_seconds_remaining_never_negative(self):
        self.assertEqual(self.tracker.seconds_remaining(KNOWN_START + 9999), 0.0)


class TestBoundaryRoll(unittest.TestCase):
    def setUp(self):
        self.twap = FakeTwap()
        self.tracker = FiveMinuteTracker(twap_fn=self.twap, start=KNOWN_START)

    def test_roll_emits_completed_window(self):
        self.tracker.add_tick(100.0, KNOWN_START + 1)
        self.tracker.add_tick(105.0, KNOWN_START + 100)
        result = self.tracker.add_tick(105.0, KNOWN_START + WINDOW_SECONDS)
        self.assertIsNotNone(result)
        self.assertEqual(result.start, KNOWN_START)
        self.assertEqual(result.end, KNOWN_START + WINDOW_SECONDS)
        self.assertEqual(result.open_spot, 100.0)
        self.assertEqual(result.close_spot, 105.0)
        self.assertEqual(result.outcome, "up")

    def test_roll_resets_open_for_new_window(self):
        self.tracker.add_tick(100.0, KNOWN_START + 1)
        self.tracker.add_tick(105.0, KNOWN_START + WINDOW_SECONDS)
        self.assertEqual(self.tracker.open_spot, 105.0)
        self.assertEqual(self.tracker.start, KNOWN_START + WINDOW_SECONDS)
        self.assertEqual(self.tracker.tick_count, 1)

    def test_down_outcome(self):
        self.tracker.add_tick(100.0, KNOWN_START + 1)
        self.tracker.add_tick(95.0, KNOWN_START + 50)
        result = self.tracker.add_tick(90.0, KNOWN_START + WINDOW_SECONDS)
        self.assertEqual(result.outcome, "down")
        # Close is the last trade inside the window (95), not the boundary tick.
        self.assertEqual(result.close_spot, 95.0)
        self.assertAlmostEqual(result.change_bps, -500.0, places=6)

    def test_boundary_tick_opens_next_window_not_closes_current(self):
        # A trade stamped exactly at the boundary belongs to the *new* window.
        # The close must be the last trade strictly inside the old window, or
        # every label would be contaminated by the next window's opening move.
        self.tracker.add_tick(100.0, KNOWN_START + 1)
        result = self.tracker.add_tick(95.0, KNOWN_START + WINDOW_SECONDS)
        self.assertEqual(result.open_spot, 100.0)
        self.assertEqual(result.close_spot, 100.0)
        self.assertEqual(result.outcome, "up")
        self.assertEqual(self.tracker.open_spot, 95.0)

    def test_one_tick_before_boundary_is_the_close(self):
        self.tracker.add_tick(100.0, KNOWN_START + 1)
        self.tracker.add_tick(90.0, KNOWN_START + 299.999)
        result = self.tracker.add_tick(120.0, KNOWN_START + WINDOW_SECONDS)
        self.assertEqual(result.close_spot, 90.0)
        self.assertEqual(result.outcome, "down")

    def test_tie_resolves_up(self):
        self.tracker.add_tick(100.0, KNOWN_START + 1)
        self.tracker.add_tick(100.0, KNOWN_START + 50)
        result = self.tracker.add_tick(100.0, KNOWN_START + WINDOW_SECONDS)
        self.assertEqual(result.outcome, "up")
        self.assertEqual(result.change_bps, 0.0)

    def test_roll_carries_high_low_vwap(self):
        self.tracker.add_tick(100.0, KNOWN_START + 1)
        self.tracker.add_tick(120.0, KNOWN_START + 50)
        self.tracker.add_tick(80.0, KNOWN_START + 100)
        result = self.tracker.add_tick(90.0, KNOWN_START + WINDOW_SECONDS)
        self.assertEqual(result.high, 120.0)
        self.assertEqual(result.low, 80.0)
        self.assertIsNotNone(result.vwap)
    def test_thruster_crosses_boundary_exactly(self):
        self.tracker.add_tick(100.0, KNOWN_START + 299.9)
        result = self.tracker.add_tick(110.0, KNOWN_START + 300)
        self.assertIsNotNone(result)
        self.assertEqual(result.start, KNOWN_START)

    def test_one_second_before_boundary_does_not_roll(self):
        self.tracker.add_tick(100.0, KNOWN_START + 1)
        self.assertIsNone(self.tracker.add_tick(101.0, KNOWN_START + 299))

    def test_skipped_windows_advance_without_inventing_data(self):
        self.tracker.add_tick(100.0, KNOWN_START + 1)
        # Jump three windows ahead as if the process were suspended.
        result = self.tracker.add_tick(200.0, KNOWN_START + 3 * WINDOW_SECONDS)
        self.assertIsNotNone(result)
        self.assertEqual(result.start, KNOWN_START)
        self.assertEqual(self.tracker.start, KNOWN_START + 3 * WINDOW_SECONDS)
        self.assertEqual(self.tracker.open_spot, 200.0)

    def test_gap_flagged_on_sparse_window(self):
        for i in range(5):
            self.tracker.add_tick(100.0 + i, KNOWN_START + i)
        result = self.tracker.add_tick(105.0, KNOWN_START + WINDOW_SECONDS)
        self.assertTrue(result.gap_detected)

    def test_gap_not_flagged_on_dense_window(self):
        for i in range(100):
            self.tracker.add_tick(100.0 + (i % 5), KNOWN_START + i * 3)
        result = self.tracker.add_tick(105.0, KNOWN_START + WINDOW_SECONDS)
        self.assertFalse(result.gap_detected)

    def test_gap_flagged_on_long_stall_despite_many_ticks(self):
        # Plenty of ticks, but a 30s hole in the middle means we missed prints.
        for i in range(12):
            self.tracker.add_tick(100.0, KNOWN_START + i * 0.5)
        for i in range(12):
            self.tracker.add_tick(101.0, KNOWN_START + 40 + i * 0.5)
        result = self.tracker.add_tick(102.0, KNOWN_START + WINDOW_SECONDS)
        self.assertEqual(result.tick_count, 24)
        self.assertGreater(result.max_tick_gap_s, 30.0)
        self.assertTrue(result.gap_detected)

    def test_calm_but_continuous_window_not_flagged(self):
        # 30 ticks every 4s over 5 minutes is a quiet market, not a gap.
        for i in range(29):
            self.tracker.add_tick(100.0, KNOWN_START + i * 4)
        result = self.tracker.add_tick(100.0, KNOWN_START + 116)
        result = self.tracker.add_tick(100.0, KNOWN_START + WINDOW_SECONDS)
        self.assertFalse(result.gap_detected)
        self.assertTrue(result.usable_for_training)

    def test_max_tick_gap_reported(self):
        for i in range(4):
            self.tracker.add_tick(100.0, KNOWN_START + i * 2.5)
        result = self.tracker.add_tick(100.0, KNOWN_START + WINDOW_SECONDS)
        self.assertAlmostEqual(result.max_tick_gap_s, 2.5, places=6)

    def test_result_serialises_with_slug(self):
        self.tracker.add_tick(100.0, KNOWN_START + 1)
        self.tracker.add_tick(105.0, KNOWN_START + 50)
        result = self.tracker.add_tick(200.0, KNOWN_START + WINDOW_SECONDS)
        payload = result.to_dict()
        self.assertEqual(payload["type"], "round")
        self.assertEqual(payload["slug"], market_slug(KNOWN_START))
        self.assertEqual(payload["outcome"], "up")
        self.assertEqual(payload["venue"], "binance")
        self.assertAlmostEqual(payload["change_bps"], 500.0, places=6)

    def test_consecutive_rolls_chain(self):
        seen = []
        price = 100.0
        for window in range(4):
            start = KNOWN_START + window * WINDOW_SECONDS
            self.tracker.add_tick(price, start + 1)
            price += 5
            result = self.tracker.add_tick(price, start + WINDOW_SECONDS)
            seen.append((result.start, result.outcome))
        self.assertEqual(
            seen,
            [
                (KNOWN_START, "up"),
                (KNOWN_START + 300, "up"),
                (KNOWN_START + 600, "up"),
                (KNOWN_START + 900, "up"),
            ],
        )


class TestFlush(unittest.TestCase):
    def setUp(self):
        self.twap = FakeTwap()
        self.tracker = FiveMinuteTracker(twap_fn=self.twap, start=KNOWN_START)

    def test_flush_without_data_returns_none(self):
        self.assertIsNone(self.tracker.flush(KNOWN_START + 100))

    def test_flush_closes_open_window(self):
        self.tracker.add_tick(100.0, KNOWN_START + 1)
        self.tracker.add_tick(110.0, KNOWN_START + 100)
        result = self.tracker.flush(KNOWN_START + 150)
        self.assertIsNotNone(result)
        self.assertEqual(result.outcome, "up")
        self.assertEqual(result.close_spot, 110.0)

    def test_flushed_window_is_marked_partial(self):
        # A shutdown-truncated window has a close price that is simply the last
        # trade we saw, not the window's real close. Labelling it would poison
        # the training set, so it must be excluded.
        for i in range(50):
            self.tracker.add_tick(100.0 + i, KNOWN_START + i * 4)
        result = self.tracker.flush(KNOWN_START + 200)
        self.assertTrue(result.partial)
        self.assertFalse(result.usable_for_training)
        self.assertFalse(result.gap_detected, "50 ticks is dense, so not a gap")

    def test_boundary_roll_is_not_partial(self):
        for i in range(30):
            self.tracker.add_tick(100.0, KNOWN_START + i * 4)
        result = self.tracker.add_tick(105.0, KNOWN_START + WINDOW_SECONDS)
        self.assertFalse(result.partial)
        self.assertTrue(result.usable_for_training)

    def test_partial_flag_serialises(self):
        self.tracker.add_tick(100.0, KNOWN_START + 1)
        payload = self.tracker.flush(KNOWN_START + 50).to_dict()
        self.assertTrue(payload["partial"])
        self.assertFalse(payload["usable_for_training"])

    def test_gap_window_is_also_unusable(self):
        for i in range(3):
            self.tracker.add_tick(100.0, KNOWN_START + i)
        result = self.tracker.add_tick(105.0, KNOWN_START + WINDOW_SECONDS)
        self.assertTrue(result.gap_detected)
        self.assertFalse(result.usable_for_training)


class TestLateOpen(unittest.TestCase):
    """A window we joined mid-flight has no real open, so it must be excluded."""

    def setUp(self):
        self.twap = FakeTwap()
        self.tracker = FiveMinuteTracker(twap_fn=self.twap, start=KNOWN_START)

    def _fill(self, step: float = 1.0, count: int = 60) -> None:
        for i in range(count):
            self.tracker.add_tick(100.0, KNOWN_START + 240 + i * step)

    def test_joining_30s_late_is_flagged(self):
        self._fill()
        result = self.tracker.add_tick(105.0, KNOWN_START + WINDOW_SECONDS)
        self.assertAlmostEqual(result.open_delay_s, 240.0, places=6)
        self.assertTrue(result.late_open)
        self.assertFalse(result.usable_for_training)

    def test_joining_just_after_the_boundary_is_fine(self):
        for i in range(60):
            self.tracker.add_tick(100.0, KNOWN_START + i * 4)
        result = self.tracker.add_tick(105.0, KNOWN_START + WINDOW_SECONDS)
        self.assertLess(result.open_delay_s, 5.0)
        self.assertFalse(result.late_open)
        self.assertTrue(result.usable_for_training)

    def test_open_delay_boundary_is_exactly_five_seconds(self):
        self.tracker.add_tick(100.0, KNOWN_START + 5.0)
        result = self.tracker.add_tick(101.0, KNOWN_START + WINDOW_SECONDS)
        self.assertFalse(result.late_open, "5.0s is within tolerance")

    def test_late_open_flag_resets_for_next_window(self):
        # Started late, but the following window is observed from its first tick.
        self._fill()
        self.tracker.add_tick(105.0, KNOWN_START + WINDOW_SECONDS)
        for i in range(60):
            self.tracker.add_tick(105.0, KNOWN_START + WINDOW_SECONDS + i * 4)
        result = self.tracker.add_tick(110.0, KNOWN_START + 2 * WINDOW_SECONDS)
        self.assertFalse(result.late_open)
        self.assertEqual(result.open_delay_s, 0.0)

    def test_late_open_serialises(self):
        self._fill()
        payload = self.tracker.add_tick(105.0, KNOWN_START + WINDOW_SECONDS).to_dict()
        self.assertTrue(payload["late_open"])
        self.assertFalse(payload["usable_for_training"])
        self.assertGreater(payload["open_delay_s"], 5.0)


class TestRealClockSanity(unittest.TestCase):
    def test_default_start_is_current_window(self):
        tracker = FiveMinuteTracker()
        self.assertEqual(tracker.start, window_start(time.time()))
        self.assertEqual(tracker.start % WINDOW_SECONDS, 0)


class TestBoundaryWait(unittest.TestCase):
    """The wait must not overshoot into an extra five-minute window."""

    def test_target_boundary_is_the_next_window_start(self):
        for offset in (0.0, 1.0, 123.4, 299.9):
            now = KNOWN_START + offset
            self.assertEqual(window_end(window_start(now)), KNOWN_START + WINDOW_SECONDS)

    def test_wait_does_not_overshoot_the_boundary(self):
        # A quarter-second before the roll, with each poll jumping 0.5s: the
        # first poll lands *past* the boundary. Re-deriving the window from that
        # reading would point at the window after next and idle for another five
        # minutes, so the target has to be pinned before the wait starts.
        clock = FakeClock(KNOWN_START + 299.75, step=0.5, freeze_first=True)
        engine = make_bare_engine(clock, KNOWN_START)

        started = time.time()
        engine._wait_for_boundary()

        self.assertLess(time.time() - started, 3.0, "must not idle for a whole extra window")
        self.assertLessEqual(clock.steps, 2, "should break out on the first poll past the boundary")
        self.assertEqual(engine.tracker.start, KNOWN_START + WINDOW_SECONDS)

    def test_wait_survives_a_large_clock_jump(self):
        # If each poll advanced the clock by a whole window, a naive loop that
        # re-derives its target would still converge, but slowly. Pinning the
        # target means exactly one poll is needed after the boundary passes.
        clock = FakeClock(KNOWN_START + 1.0, step=1.0, freeze_first=True)
        engine = make_bare_engine(clock, KNOWN_START)
        clock.t = KNOWN_START + WINDOW_SECONDS - 0.5
        started = time.time()
        engine._wait_for_boundary()
        self.assertLess(time.time() - started, 2.0)
        self.assertEqual(engine.tracker.start, KNOWN_START + WINDOW_SECONDS)

    def test_wait_is_interruptible(self):
        clock = FakeClock(KNOWN_START + 1.0, step=0.5, freeze_first=True)
        engine = make_bare_engine(clock, KNOWN_START)
        engine._stop.set()
        started = time.time()
        engine._wait_for_boundary()  # must not block for the remaining ~5 minutes
        self.assertLess(time.time() - started, 1.0)


if __name__ == "__main__":
    unittest.main()
