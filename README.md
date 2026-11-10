# BTC live price feed

Tooling to build a labelled dataset of Polymarket's BTC Up/Down 5-minute markets,
so the AI agent strategies can be built and calibrated on real data instead of
guesses.

## The three important facts this is built around

**1. The markets do not resolve on Binance.** Polymarket's BTC 5m markets resolve
against Chainlink's BTC/USD data stream, and the newer ones use the
**BTC/USD TWAP 60s** stream. Binance is a high-fidelity *proxy*, not the source
of truth. This is why the tool computes a true 60-second time-weighted average
alongside spot, and why a second venue runs in parallel to measure the residual
error. Anything that trades these markets should treat our labels as a proxy and
keep measuring the gap.

**2. Windows are epoch-aligned.** Markets are `btc-updown-5m-<start_epoch>` where
`start_epoch % 300 == 0`. Up resolves when `close >= open` — ties resolve **Up**,
not a coin flip. A trade stamped exactly at a boundary belongs to the *next*
window; the close is the last trade strictly inside the window.

**3. Clock accuracy is a correctness requirement.** Windows are defined in
exchange-server time. A local clock 200ms fast means every frozen open price was
sampled at the wrong instant, and the error is invisible in the output. So the
clock offset is measured at startup and all window math uses corrected time.

## Folders

| Folder | What it is |
|---|---|
| `1-minimal/` | Single-file proof of concept. Proves the data is free and real. |
| `2-full/` | The real tool: feed engine, window tracker, recorder, dashboard, tests, verifier. |
| `3-headless/` | 24/7 daemon over the same engine. No TTY, just data on disk. |

Folders 2 and 3 share one `feed` library on purpose, so the labels the dashboard
shows and the labels the daemon records cannot drift apart.

## Quick start

No dependencies. Python 3.11+ standard library only.

```bash
# 1. prove it works (~10 seconds)
python3 1-minimal/price.py --count 10

# 2. run the tests
cd 2-full && python3 -m unittest discover -s tests

# 3. live dashboard
cd 2-full && python3 -m feed.cli

# 4. or record in the background for days
cd 3-headless && python3 daemon.py --data-dir ../data --duration 86400

# 5. check what you collected
cd 3-headless && python3 daemon.py --status --data-dir ../data

# 6. independently verify the labels against Binance REST klines
cd 2-full && python3 verify.py --data-dir ../data
```

## What gets recorded

`data/rounds.jsonl` — one line per completed 5-minute window. This is the
training set. Each entry carries the open/close (spot *and* TWAP), high/low/VWAP,
tick count, longest stall, the resolved outcome, and three quality flags. Any
flag set means the round is excluded from training via `usable_for_training`:

- `late_open` — our first tick arrived more than 5s into the window, so we never
  saw the market's real open. The recorded open is just the first price we
  happened to see. This is the flag that catches a restart landing mid-window.
- `gap_detected` — too few ticks, or a stall over 5s long enough that we likely
  missed prints.
- `partial` — the window was truncated by a shutdown, so the "close" is merely
  the last trade we saw.

`data/ticks-YYYY-MM-DD.jsonl` — every raw trade, for replay.

`data/events.jsonl` — startup, clock offset, shutdown.

## Verifying the labels

The label for each window is a direct claim about what the market saw, so it
needs checking against something independent. `verify.py` re-derives every window
from Binance's REST 1m klines over a different transport and compares open, close,
and outcome. Run it after any soak:

```bash
cd 2-full && python3 verify.py --data-dir ../data
```

Tolerances are in basis points, not ticks — a sub-tick difference in the open is
noise, but a flipped outcome is a real failure and is always reported. Flagged
rounds are counted and reported separately rather than silently passed, since the
point is to notice labels we should not trust.

**This has already earned its keep.** It caught a real bug: a daemon that started
mid-window was recording its first observed price as the window open, producing
an open that was 5.8 bps wrong while the close and the outcome both looked
perfectly fine. That is precisely the class of error that silently poisons a
training set, and nothing in the recorded data would have revealed it. Hence the
`late_open` flag and the daemon's default wait-for-boundary startup.

## Known limits

- **Proxy, not oracle.** We track Binance. The Chainlink stream aggregates venues
  and, for the newest markets, applies a 60s TWAP. The Coinbase basis shown in
  the dashboard bounds the cross-venue component of that error, but not the
  Chainlink-specific part. This is the main open risk in the project.
- **REST verification is itself approximate.** Kline opens/closes are one-minute
  aggregates, so a genuine sub-minute discrepancy is expected within tolerance.
- **Polymarket API is unreachable from this machine** (`clob.polymarket.com` and
  friends fail to route), so no market discovery, order book, or fills logic here
  yet. Everything above is venue-agnostic price capture, which is the part that
  had to be right before any of that is worth building.
- **Leverage**: these are $1-payout binary markets, so there is no leverage
  primitive to exploit. Position sizing is the only lever. Unresolved, per
  `Idea.md`.
