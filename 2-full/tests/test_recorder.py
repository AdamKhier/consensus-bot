"""Recorder and sparkline tests using a temp directory."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from feed.engine import render_sparkline  # noqa: E402
from feed.recorder import Recorder, load_rounds  # noqa: E402
from feed.types import Side, Tick  # noqa: E402
from feed.windows import FiveMinuteTracker  # noqa: E402

KNOWN_START = 1790487300


def make_tick(price: float = 100.0) -> Tick:
    now = int(time.time() * 1000)
    return Tick("binance", "BTCUSDT", price, 0.01, Side.BUY, now, now)


class TestRecorder(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="feedtest-")
        self.rec = Recorder(self.dir)

    def tearDown(self):
        self.rec.close()

    def test_creates_data_dir(self):
        path = os.path.join(self.dir, "nested", "deeper")
        rec = Recorder(path)
        rec.close()
        self.assertTrue(os.path.isdir(path))

    def test_writes_tick_line(self):
        self.rec.write_tick(make_tick(123.45))
        day = time.strftime("%Y-%m-%d", time.gmtime())
        path = os.path.join(self.dir, f"ticks-{day}.jsonl")
        with open(path) as handle:
            payload = json.loads(handle.readline())
        self.assertEqual(payload["price"], 123.45)
        self.assertEqual(payload["venue"], "binance")
        self.assertEqual(payload["side"], "buy")
        self.assertEqual(self.rec.ticks_written, 1)

    def test_write_round_appends(self):
        tracker = FiveMinuteTracker(start=KNOWN_START)
        tracker.add_tick(100.0, KNOWN_START + 1)
        result = tracker.add_tick(105.0, KNOWN_START + 300)
        self.rec.write_round(result)
        self.rec.write_round(result)
        rounds = load_rounds(self.rec.rounds_path)
        self.assertEqual(len(rounds), 2)
        self.assertEqual(rounds[0]["outcome"], "up")

    def test_event_logged(self):
        self.rec.write_event("startup", pid=123)
        path = os.path.join(self.dir, "events.jsonl")
        with open(path) as handle:
            payload = json.loads(handle.readline())
        self.assertEqual(payload["type"], "startup")
        self.assertEqual(payload["pid"], 123)
        self.assertIn("ts", payload)

    def test_no_ticks_mode_writes_nothing(self):
        # Fresh directory: setUp's recorder already created today's tick file here.
        empty_dir = tempfile.mkdtemp(prefix="feedtest-noticks-")
        rec = Recorder(empty_dir, record_ticks=False)
        rec.write_tick(make_tick())
        rec.close()
        self.assertEqual(rec.ticks_written, 0)
        self.assertFalse(os.listdir(empty_dir))

    def test_writes_after_close_are_ignored(self):
        self.rec.close()
        self.rec.write_tick(make_tick())
        self.rec.write_event("late")
        self.assertEqual(self.rec.ticks_written, 0)

    def test_load_rounds_missing_file(self):
        self.assertEqual(load_rounds(os.path.join(self.dir, "nope.jsonl")), [])

    def test_load_rounds_skips_corrupt_line(self):
        path = os.path.join(self.dir, "rounds.jsonl")
        with open(path, "w") as handle:
            handle.write('{"outcome": "up"}\n')
            handle.write("not json\n")
            handle.write('{"outcome": "down"}\n')
            handle.write('{"truncated": ')  # simulate a kill mid-write
        rounds = load_rounds(path)
        self.assertEqual([r["outcome"] for r in rounds], ["up", "down"])

    def test_context_manager(self):
        with Recorder(self.dir) as rec:
            rec.write_event("x")
        self.assertTrue(rec._closed)


class TestSparkline(unittest.TestCase):
    def test_flat_series_is_a_line(self):
        self.assertEqual(render_sparkline([100.0] * 10), "─" * 10)

    def test_empty_series(self):
        self.assertEqual(render_sparkline([]), "")
    def test_single_point(self):
        self.assertEqual(render_sparkline([100.0]), "")

    def test_rising_series_ends_high(self):
        spark = render_sparkline([float(i) for i in range(20)])
        self.assertEqual(len(spark), 20)
        self.assertEqual(spark[-1], "█")
        self.assertEqual(spark[0], "▁")

    def test_downsample_to_width(self):
        spark = render_sparkline([float(i) for i in range(500)], width=50)
        self.assertEqual(len(spark), 50)

    def test_uses_only_valid_levels(self):
        spark = render_sparkline([1.0, 2.0, 3.0, 4.0])
        self.assertTrue(all(ch in "▁▂▃▄▅▆▇█" for ch in spark))


if __name__ == "__main__":
    unittest.main()
