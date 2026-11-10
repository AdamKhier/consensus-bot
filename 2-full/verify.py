#!/usr/bin/env python3
"""Validate recorded rounds against Binance's own REST klines.

The whole project rests on one assumption: that the open/close we freeze from the
WebSocket feed matches what the market would actually have seen. This script
tests that assumption independently, by re-deriving each window from a different
transport (REST klines) and comparing.

A mismatch here is not a rounding artefact. It means our labels are wrong, and
every strategy trained on them inherits the error.

    python3 verify.py --data-dir ../data
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "2-full"))

from feed.recorder import load_rounds  # noqa: E402
from feed.windows import WINDOW_SECONDS  # noqa: E402

KLINES_URL = "https://api.binance.com/api/v3/klines"


def fetch_klines(start: int, end: int, symbol: str = "BTCUSDT") -> list[list]:
    url = f"{KLINES_URL}?symbol={symbol}&interval=1m&startTime={start * 1000}&endTime={end * 1000}&limit=1000"
    request = urllib.request.Request(url, headers={"User-Agent": "consensus-bot/1.0"})
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read())


def kline_bounds(rows: list[list]) -> tuple[float, float] | None:
    """(first open, last close) across the 1m klines covering a window."""
    if not rows:
        return None
    return float(rows[0][1]), float(rows[-1][4])


def verify(data_dir: str, symbol: str, tolerance_bps: float) -> int:
    rounds = load_rounds(os.path.join(data_dir, "rounds.jsonl"))
    if not rounds:
        print("no rounds to verify")
        return 1

    checked = mismatch = skipped = 0
    known_bad = []
    failures = []

    for record in rounds:
        if record.get("open_spot") is None or record.get("close_spot") is None:
            skipped += 1
            continue

        start, end = record["start"], record["end"]

        # A window we joined late, truncated, or lost data on cannot be held to
        # the same standard: we never claimed its open was the market's open.
        if not record.get("usable_for_training", True):
            reason = (
                "late_open" if record.get("late_open")
                else "partial" if record.get("partial")
                else "gap_detected"
            )
            known_bad.append((start, reason))
            continue

        try:
            rows = fetch_klines(start, end - 1, symbol)
        except OSError as exc:
            print(f"  fetch failed for {start}: {exc}")
            skipped += 1
            continue

        bounds = kline_bounds(rows)
        if bounds is None:
            skipped += 1
            continue
        rest_open, rest_close = bounds

        checked += 1
        open_bps = (rest_open - record["open_spot"]) / record["open_spot"] * 10_000
        close_bps = (rest_close - record["close_spot"]) / record["close_spot"] * 10_000

        # Outcome is what actually matters; a sub-tick open difference is noise.
        rest_outcome = "up" if rest_close >= rest_open else "down"
        outcome_ok = rest_outcome == record.get("outcome")
        price_ok = abs(open_bps) <= tolerance_bps and abs(close_bps) <= tolerance_bps

        if not (outcome_ok and price_ok):
            mismatch += 1
            failures.append(
                {
                    "start": start,
                    "ours": f"{record['outcome']} open={record['open_spot']} close={record['close_spot']}",
                    "rest": f"{rest_outcome} open={rest_open} close={rest_close}",
                    "open_diff_bps": round(open_bps, 3),
                    "close_diff_bps": round(close_bps, 3),
                    "outcome_ok": outcome_ok,
                }
            )
        time.sleep(0.25)  # stay well clear of the REST rate limit

    print(f"rounds in file     : {len(rounds)}")
    print(f"verified           : {checked}")
    print(f"excluded (flagged) : {len(known_bad)}")
    print(f"skipped (no data)  : {skipped}")
    print(f"mismatches         : {mismatch}  (tolerance {tolerance_bps} bps)")

    if known_bad:
        counts: dict[str, int] = {}
        for _start, reason in known_bad:
            counts[reason] = counts.get(reason, 0) + 1
        summary = ", ".join(f"{count} {reason}" for reason, count in sorted(counts.items()))
        print(f"\nexcluded because: {summary}")
        print("  (expected: the first round after any start/restart, plus any feed drop)")

    if failures:
        print("\nMISMATCHES on rounds we stand behind:")
        for f in failures:
            print(
                f"  {f['start']}  outcome_ok={f['outcome_ok']}  "
                f"open {f['open_diff_bps']:+.2f}bps  close {f['close_diff_bps']:+.2f}bps"
            )
            print(f"      ours: {f['ours']}")
            print(f"      rest: {f['rest']}")
        return 1

    if checked:
        print("\nall verified rounds agree with Binance REST klines")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify recorded rounds against REST klines")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--tolerance-bps", type=float, default=1.0)
    args = parser.parse_args(argv)
    return verify(args.data_dir, args.symbol, args.tolerance_bps)


if __name__ == "__main__":
    raise SystemExit(main())
