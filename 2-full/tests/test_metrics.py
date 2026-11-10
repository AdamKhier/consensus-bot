"""TWAP / rolling-metric tests against hand-computed values."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from feed.metrics import (  # noqa: E402
    PriceHistory,
    RollingMean,
    RollingWindow,
    TimeWeightedAverage,
)


class TestTimeWeightedAverage(unittest.TestCase):
    def test_flat_price_twap_equals_price(self):
        twap = TimeWeightedAverage(60.0)
        for t in range(0, 61, 10):
            twap.add(100.0, ts=float(t))
        result = twap.result(now=60.0)
        self.assertAlmostEqual(result.value, 100.0, places=9)

    def test_two_levels_weighted_by_duration(self):
        # 100 for 30s, then 200 for 30s -> integral 100*30 + 200*30 = 9000
        # over 60s total -> 150.
        twap = TimeWeightedAverage(60.0)
        twap.add(100.0, ts=0.0)
        twap.add(200.0, ts=30.0)
        result = twap.result(now=60.0)
        self.assertAlmostEqual(result.value, 150.0, places=9)
        self.assertAlmostEqual(result.coverage, 1.0, places=6)

    def test_duration_weighting_not_sample_weighting(self):
        # 10 rapid ticks at 100 then one tick at 200 held for 50s. A naive mean
        # would say ~109; the TWAP must be dominated by the long 200 plateau.
        twap = TimeWeightedAverage(60.0)
        for i in range(10):
            twap.add(100.0, ts=i * 0.1)
        twap.add(200.0, ts=1.0)
        result = twap.result(now=51.0)
        expected = (100.0 * 1.0 + 200.0 * 50.0) / 51.0
        self.assertAlmostEqual(result.value, expected, places=6)
        self.assertGreater(result.value, 190.0)

    def test_three_level_weighted_average(self):
        twap = TimeWeightedAverage(60.0)
        twap.add(10.0, ts=0.0)
        twap.add(20.0, ts=20.0)
        twap.add(30.0, ts=50.0)
        result = twap.result(now=60.0)
        expected = (10.0 * 20 + 20.0 * 30 + 30.0 * 10) / 60.0
        self.assertAlmostEqual(result.value, expected, places=9)

    def test_partial_coverage_flagged(self):
        twap = TimeWeightedAverage(60.0)
        twap.add(500.0, ts=100.0)
        twap.add(500.0, ts=105.0)
        result = twap.result(now=105.0)
        self.assertLess(result.coverage, 0.2)
        self.assertFalse(result.valid)
        self.assertAlmostEqual(result.value, 500.0, places=9)

    def test_full_coverage_is_valid(self):
        twap = TimeWeightedAverage(60.0)
        twap.add(100.0, ts=0.0)
        twap.add(101.0, ts=59.0)
        self.assertTrue(twap.result(now=60.0).valid)

    def test_out_of_order_tick_does_not_corrupt_integral(self):
        twap = TimeWeightedAverage(60.0)
        twap.add(100.0, ts=30.0)
        twap.add(200.0, ts=10.0)  # stale feed, arrives late
        twap.add(300.0, ts=40.0)
        result = twap.result(now=60.0)
        self.assertIsNotNone(result.value)
        self.assertTrue(0 < result.value < 1000)

    def test_old_points_evicted(self):
        twap = TimeWeightedAverage(10.0)
        for t in range(0, 200, 5):
            twap.add(float(t), ts=float(t))
        result = twap.result(now=200.0)
        self.assertLessEqual(len(twap._points), 5)
        # Only t=190 and t=195 survive, extended to 200: (190*5 + 195*5) / 10.
        self.assertAlmostEqual(result.value, 192.5, places=6)

    def test_single_point_returns_that_price(self):
        twap = TimeWeightedAverage(60.0)
        twap.add(42.0, ts=0.0)
        self.assertEqual(twap.result(now=0.0).value, 42.0)

    def test_no_points_returns_none(self):
        self.assertIsNone(TimeWeightedAverage(60.0).result(now=0.0).value)

    def test_zero_and_negative_prices_rejected(self):
        twap = TimeWeightedAverage(60.0)
        twap.add(0.0, ts=0.0)
        twap.add(-5.0, ts=1.0)
        self.assertIsNone(twap.result(now=1.0).value)


class TestRollingMean(unittest.TestCase):
    def test_mean_of_values(self):
        mean = RollingMean(10)
        for v in (10.0, 20.0, 30.0):
            mean.add(v)
        self.assertAlmostEqual(mean.value(), 20.0, places=9)

    def test_window_slides(self):
        mean = RollingMean(3)
        for v in (1.0, 2.0, 3.0, 4.0):
            mean.add(v)
        self.assertEqual(len(mean), 3)
        self.assertAlmostEqual(mean.value(), 3.0, places=9)

    def test_empty_is_none(self):
        self.assertIsNone(RollingMean(5).value())


class TestPriceHistory(unittest.TestCase):
    def test_high_low_within_range(self):
        history = PriceHistory()
        for t, p in ((0.0, 10.0), (1.0, 15.0), (2.0, 5.0), (3.0, 12.0)):
            history.add(p, ts=t)
        low, high = history.high_low(0.0, 3.0)
        self.assertEqual(low, 5.0)
        self.assertEqual(high, 15.0)

    def test_range_excludes_end_boundary(self):
        history = PriceHistory()
        history.add(10.0, ts=0.0)
        history.add(99.0, ts=300.0)
        low, high = history.high_low(0.0, 300.0)
        self.assertEqual((low, high), (10.0, 10.0))

    def test_empty_range(self):
        self.assertEqual(PriceHistory().high_low(0.0, 10.0), (None, None))

    def test_last_n_filters_by_time(self):
        history = PriceHistory()
        for t in range(10):
            history.add(float(t), ts=float(t))
        # Strictly greater than now - 5, so t = 6..9.
        self.assertEqual(len(history.last_n(5, now=10.0)), 4)
        self.assertEqual(history.last_n(5, now=10.0)[0], 6.0)


class TestRollingWindow(unittest.TestCase):
    def test_tracks_high_low_vwap(self):
        window = RollingWindow()
        for t, p in ((0.0, 100.0), (1.0, 120.0), (2.0, 90.0), (3.0, 110.0)):
            window.add(p, ts=t)
        self.assertEqual(window.high, 120.0)
        self.assertEqual(window.low, 90.0)
        # vwap integrates from the first tick: 100*1 + 120*1 + 90*1 over 3s
        self.assertAlmostEqual(window.vwap, (100 + 120 + 90) / 3, places=9)

    def test_reset_clears_state(self):
        window = RollingWindow()
        window.add(100.0, ts=0.0)
        window.reset()
        self.assertIsNone(window.high)
        self.assertIsNone(window.vwap)

    def test_vwap_none_before_second_tick(self):
        window = RollingWindow()
        window.add(100.0, ts=0.0)
        self.assertIsNone(window.vwap)


if __name__ == "__main__":
    unittest.main()
