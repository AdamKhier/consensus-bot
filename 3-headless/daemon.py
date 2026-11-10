#!/usr/bin/env python3
"""Headless 24/7 recorder - no TTY, no dashboard, just data on disk.

Same engine as the interactive tool, so the labels produced here are identical
to the ones you watch in 2-full. Intended to run under systemd, tmux or nohup
for days at a time, since the value of this project is in the accumulated
labelled dataset rather than any single run.

    python3 daemon.py --data-dir ../data
    python3 daemon.py --duration 3600        # one hour
    python3 daemon.py --no-ticks              # rounds only, tiny files
    python3 daemon.py --status                # summarise what is recorded
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "2-full"))

from feed.engine import Engine  # noqa: E402
from feed.recorder import load_rounds  # noqa: E402
from feed.windows import market_slug  # noqa: E402


def log(message: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


def show_status(data_dir: str) -> int:
    rounds = load_rounds(os.path.join(data_dir, "rounds.jsonl"))
    if not rounds:
        log(f"no rounds recorded in {data_dir}")
        return 1

    # Only count outcomes for rounds we can actually stand behind. A truncated
    # or late-joined window has an outcome that is not a real label, and mixing
    # it into an up/down tally would quietly poison any strategy built on it.
    usable = [r for r in rounds if r.get("usable_for_training", True)]
    excluded = [r for r in rounds if not r.get("usable_for_training", True)]
    ups = [r for r in usable if r.get("outcome") == "up"]
    downs = [r for r in usable if r.get("outcome") == "down"]

    log(f"rounds recorded : {len(rounds)}")
    log(f"  usable        : {len(usable)}")
    log(f"    up          : {len(ups)}")
    log(f"    down        : {len(downs)}")
    if excluded:
        reasons: dict[str, int] = {}
        for r in excluded:
            reason = (
                "late_open" if r.get("late_open")
                else "partial" if r.get("partial")
                else "gap_detected"
            )
            reasons[reason] = reasons.get(reason, 0) + 1
        summary = ", ".join(f"{n} {reason}" for reason, n in sorted(reasons.items()))
        log(f"  excluded      : {len(excluded)}  ({summary})")

    basis = [r["change_bps"] for r in usable if r.get("change_bps") is not None]
    if basis:
        log(f"  change bps    : min {min(basis):+.2f}  max {max(basis):+.2f}")
    if usable:
        log(f"  first         : {market_slug(usable[0]['start'])}")
        log(f"  last          : {market_slug(usable[-1]['start'])}")

    print("\nlast 10 rounds:")
    print(f"  {'slug':<28} {'outcome':<8} {'bps':>9} {'ticks':>7}")
    for r in rounds[-10:]:
        bps = r.get("change_bps")
        bps_text = f"{bps:+.2f}" if bps is not None else "-"
        if not r.get("usable_for_training", True):
            flag = "  [excluded]"
        elif r.get("gap_detected"):
            flag = "  [gap]"
        else:
            flag = ""
        print(
            f"  {market_slug(r['start']):<28} {str(r.get('outcome')):<8} "
            f"{bps_text:>9} {r.get('tick_count', 0):>7}{flag}"
        )
    if excluded:
        print("\n  [excluded] rounds are missing from the up/down tally above")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Headless BTC feed recorder")
    parser.add_argument("--symbol", default="btcusdt")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--no-ticks", action="store_true", help="record rounds only")
    parser.add_argument("--no-coinbase", action="store_true")
    parser.add_argument("--duration", type=float, default=0, help="exit after N seconds")
    parser.add_argument(
        "--no-wait",
        action="store_true",
        help="start immediately instead of idling until the next 5-minute window",
    )
    parser.add_argument("--status", action="store_true", help="summarise and exit")
    args = parser.parse_args(argv)

    if args.status:
        return show_status(args.data_dir)

    engine = Engine(
        symbol=args.symbol,
        data_dir=args.data_dir,
        record_ticks=not args.no_ticks,
        use_coinbase=not args.no_coinbase,
    )

    stopping = False

    def handle_signal(signum, _frame):
        nonlocal stopping
        stopping = True
        log(f"signal {signum} received, finishing current window...")

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    log(f"starting: symbol={args.symbol} data={args.data_dir} ticks={not args.no_ticks}")
    if args.no_wait:
        log("not waiting for a window boundary; first round will be flagged late_open")
    else:
        log("waiting for the next 5-minute window boundary (clean first open)...")
    engine.start(wait_for_boundary=not args.no_wait)
    log(
        f"clock offset {engine.clock.offset_ms:+.1f}ms "
        f"(rtt {engine.clock.rtt_ms:.0f}ms, {engine.clock.samples} samples)"
    )
    log(f"live, first window {market_slug(engine.state.window_start)}")

    started = time.time()
    last_roll_report = 0
    try:
        while not stopping:
            if args.duration and time.time() - started >= args.duration:
                log("duration reached")
                break

            result = engine.pop_round()
            if result is not None:
                bps = result.change_bps
                bps_text = f"{bps:+.2f}bps" if bps is not None else "incomplete"
                flags = ""
                if result.partial:
                    flags += "  [PARTIAL]"
                if result.gap_detected:
                    flags += "  [GAP]"
                log(
                    f"round {market_slug(result.start)} -> {str(result.outcome).upper():<4} "
                    f"{bps_text:>12}  {result.tick_count} ticks{flags}"
                )
                last_roll_report = time.time()

            if time.time() - last_roll_report > 300:
                state = engine.state
                price = f"{state.last_price:,.2f}" if state.last_price else "no ticks yet"
                log(
                    f"heartbeat: {price}  "
                    f"window {market_slug(state.window_start)}  "
                    f"{state.seconds_remaining:.0f}s left  "
                    f"{state.msg_rate:.0f} msg/s  "
                    f"ticks={engine.recorder.ticks_written} "
                    f"rounds={engine.recorder.rounds_written}"
                )
                last_roll_report = time.time()

            time.sleep(0.25)
    finally:
        engine.close()
        log(
            f"stopped: ticks={engine.recorder.ticks_written} "
            f"rounds={engine.recorder.rounds_written} -> {engine.recorder.rounds_path}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
