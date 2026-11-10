"""Terminal dashboard for the live feed.

Run with:  python -m feed.cli
"""

from __future__ import annotations

import argparse
import signal
import sys
import time

from .engine import Engine
from .windows import market_slug

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
CYAN = "\033[36m"


def _fmt(value: float | None, spec: str = ",.2f", dash: str = "-") -> str:
    return dash if value is None else format(value, spec)


def _colour_bps(value: float | None) -> str:
    if value is None:
        return DIM + "-" + RESET
    text = f"{value:+.2f}"
    return (GREEN if value >= 0 else RED) + text + RESET


def render(engine: Engine) -> str:
    state = engine.state
    lines: list[str] = []
    remaining = state.seconds_remaining
    outcome = (state.live_outcome or "?").upper()

    lines.append(f"{BOLD}BTC live feed{RESET}  {DIM}proxy for Chainlink BTC/USD{RESET}")
    lines.append("")

    price = _fmt(state.last_price)
    lines.append(f"  {BOLD}{price:>12}{RESET}  USD   last trade")
    if state.best_bid and state.best_ask:
        lines.append(
            f"  {DIM}bid {_fmt(state.best_bid)} / ask {_fmt(state.best_ask)}"
            f"  spread {_fmt(state.spread_bps, '.2f')} bps{RESET}"
        )
    latency = _fmt(state.latency_ms, '.0f', dash="-")
    lines.append(f"  {DIM}latency {latency} ms{RESET}")
    lines.append("")

    slug = market_slug(state.window_start)
    colour = GREEN if outcome == "UP" else RED if outcome == "DOWN" else DIM
    lines.append(f"  {BOLD}{slug}{RESET}")
    lines.append(
        f"  open {_fmt(state.open_spot)}   "
        f"change {_colour_bps(state.change_bps)} bps   "
        f"would resolve {colour}{outcome}{RESET}"
    )
    lines.append(
        f"  {DIM}high {_fmt(state.high)}  low {_fmt(state.low)}  vwap {_fmt(state.vwap)}{RESET}"
    )
    bar_width = 40
    filled = int(state.window_progress * bar_width)
    bar = "█" * filled + "░" * (bar_width - filled)
    lines.append(f"  {bar} {remaining:4.0f}s left")
    lines.append("")

    coverage_pct = state.twap_coverage * 100
    twap = _fmt(state.twap_60s)
    flag = "" if state.twap_coverage >= 0.9 else f" {YELLOW}(partial {coverage_pct:.0f}%){RESET}"
    lines.append(f"  {DIM}60s TWAP{RESET}  {twap}{flag}   {DIM}mean100 {_fmt(state.mean_100)}{RESET}")
    lines.append("")

    spark = engine.sparkline(300, 60)
    if spark:
        lines.append(f"  {CYAN}{spark}{RESET}")
        lines.append("")

    ws_state = "connected" if state.connected else f"{YELLOW}reconnecting{RESET}"
    lines.append(
        f"  {DIM}feed {ws_state}  {state.msg_rate:.0f} msg/s  "
        f"ticks {state.tick_count}  rounds {state.rounds}  "
        f"reconnects {state.ws_stats.reconnects}{RESET}"
    )
    if state.basis_bps is not None:
        lines.append(
            f"  {DIM}coinbase basis {_colour_bps(state.basis_bps)} bps  "
            f"clock offset {state.clock_offset_ms:+.0f} ms{RESET}"
        )
    return "\n".join(lines)


def wait_for_first_tick(engine: Engine, timeout: float = 10.0) -> bool:
    """Block until the first trade lands, or the timeout expires."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if engine.state.last_price:
            return True
        time.sleep(0.05)
    return bool(engine.state.last_price)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Live BTC feed dashboard")
    parser.add_argument("--symbol", default="btcusdt")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--no-ticks", action="store_true", help="skip raw tick recording")
    parser.add_argument("--no-coinbase", action="store_true", help="Binance only")
    parser.add_argument("--duration", type=float, default=0, help="exit after N seconds")
    parser.add_argument("--frames", type=int, default=0, help="print N frames then exit")
    args = parser.parse_args(argv)

    engine = Engine(
        symbol=args.symbol,
        data_dir=args.data_dir,
        record_ticks=not args.no_ticks,
        use_coinbase=not args.no_coinbase,
    )

    stopping = False

    def handle_signal(_signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    print("Syncing clock and connecting...", file=sys.stderr)
    engine.start()

    # Don't render the first frame until real data has landed, otherwise the
    # dashboard opens on a screen of dashes and looks broken.
    if not wait_for_first_tick(engine, timeout=10.0):
        print("warning: no trades received yet, rendering anyway", file=sys.stderr)

    started = time.time()
    frames = 0

    try:
        while not stopping:
            sys.stdout.write("\033[H\033[2J" if sys.stdout.isatty() else "\n---\n")
            print(render(engine))
            sys.stdout.flush()
            frames += 1

            if args.frames and frames >= args.frames:
                break
            if args.duration and time.time() - started >= args.duration:
                break

            # Drain completed rounds so they stay visible at the bottom of the frame.
            deadline = time.time() + 1.0
            while time.time() < deadline and not stopping:
                result = engine.pop_round()
                if result is None:
                    time.sleep(0.2)
                    continue
                if result.change_bps is None:
                    print(f"  {DIM}round closed (incomplete, discarded){RESET}")
                else:
                    print(
                        f"  {DIM}round closed {market_slug(result.start)}: "
                        f"{result.outcome.upper()} {result.change_bps:+.2f} bps "
                        f"({result.tick_count} ticks){RESET}"
                    )
    finally:
        engine.close()

    print("\nStopped. Rounds recorded to", engine.recorder.rounds_path, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
