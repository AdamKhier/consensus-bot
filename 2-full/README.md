# 2 - Full feed tool

The real thing. One WebSocket, exchange-corrected clock, rolling TWAP, 5-minute
window state machine, JSONL recorder, live dashboard, tests, and an independent
label verifier.

```bash
python3 -m unittest discover -s tests     # 110 tests, no dependencies
python3 -m feed.cli                       # live dashboard
python3 verify.py --data-dir ../data      # check labels against REST klines
```

## Modules

| File | Responsibility |
|---|---|
| `feed/ws.py` | RFC 6455 client. Handshake, frame decode, fragments, ping/pong, masked sends, reconnect with exponential backoff. |
| `feed/clock.py` | Estimates `server_time - local_time` over N round trips, takes the **median** so one slow request cannot skew it. |
| `feed/binance.py` | `aggTrade` + `bookTicker` + `depth5@100ms` on one combined stream, normalised to `Tick`. |
| `feed/coinbase.py` | Second venue. Cross-check only — never a source of truth. |
| `feed/metrics.py` | Time-weighted rolling average, rolling mean, high/low/VWAP, price history. |
| `feed/windows.py` | The 5-minute state machine. Produces the labels. |
| `feed/recorder.py` | Day-rotated JSONL, flushed per line. |
| `feed/engine.py` | Joins everything. Feed threads push, UI reads under a lock. |
| `feed/cli.py` | Terminal dashboard. |
| `verify.py` | Re-derives windows from REST klines and diffs them against the recorded labels. |

## Design decisions worth knowing about

**Why a hand-rolled WebSocket client.** There is no `pip` on this machine. The
library is ~250 lines and the tests cover frame encoding, masking, the 126/127
extended length forms, fragment reassembly, and control-frame interleaving.

**Why the median for clock sync.** Offset is estimated as
`serverTime - (t_before + t_after)/2` over several samples. A mean would be
dragged around by a single slow round trip; a 2-second stall in a mean estimate
becomes a full second of window misalignment, silently.

**Why the TWAP is time-weighted, not a mean of N ticks.** The newer markets
resolve on a 60-second TWAP, so we integrate price over time
(`sum(price_i * dt_i) / sum(dt_i)`) rather than averaging samples. A plain mean
would weight a burst of dust trades equally with one large fill, and would drift
whenever ticks arrive irregularly. There is a test
(`test_duration_weighting_not_sample_weighting`) that fails if this ever
degenerates into a sample mean.

**Why the close is the last trade *inside* the window.** A trade stamped exactly
at a boundary belongs to the next window and becomes its open. Getting this
backwards would contaminate every label with the next window's opening move —
off by one, silently, on every single round.

**Why gaps are measured by stall length, not just tick count.** A calm market
can legitimately print only 20 times in five minutes. Counting ticks alone would
flag those as feed loss and throw away good data. The tracker records the longest
gap between consecutive trades and flags a window if it exceeds 5s *or* the tick
count is under 20 — so a quiet-but-continuous window survives and a genuine hole
does not.

**Why `late_open` is a separate flag from `gap_detected` and `partial`.** Three
different ways to not know what happened, which are easy to conflate and
catastrophic to conflate:

- `late_open` — we started mid-window and never saw the real open. Found by
  `verify.py`: a daemon started 4.5 minutes into a window recorded an open 5.8
  bps wrong, with a perfectly correct close and outcome. Nothing in the recorded
  data looked wrong. Hence also `Engine(wait_for_boundary=True)` for the daemon.
- `gap_detected` — a healthy window with a hole in it.
- `partial` — a window cut short by shutdown.

Any of the three sets `usable_for_training: false`.

## The dashboard

```
BTC live feed  proxy for Chainlink BTC/USD

    84,533.30  USD   last trade
  bid 84,533.29 / ask 84,533.30  spread 0.00 bps
  latency 151 ms

  btc-updown-5m-1790487900
  open 84,533.30   change +0.00 bps   would resolve UP
  high 84,533.30  low 84,533.30  vwap 84,533.30
  ████████████████████████░░░░░░░░░░░░░░░░  116s left

  60s TWAP  84,533.30 (partial 0%)   mean100 84,533.30
  ▁▁▁▁▁▂▂▃▃▃▄▅▅▆▇█

  feed connected  19 msg/s  ticks 3  rounds 1  reconnects 0
  coinbase basis -3.81 bps  clock offset +12 ms
```

`coinbase basis` is the number that matters for the project's biggest risk: it
is the live cross-venue disagreement, which bounds how much of a Binance-vs-real
Chainlink error we can attribute to venue choice.

## Options

```
--symbol btcusdt     pair to track
--data-dir data      output directory
--no-ticks           record rounds only (tiny files)
--no-coinbase        Binance only
--frames N           render N frames then exit
--duration SECONDS   exit after a wall-clock duration
```
