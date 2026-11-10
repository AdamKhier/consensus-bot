# 3 - Headless recorder

Same engine as `../2-full`, no dashboard. Meant to run unattended for days.

The point of this project is the accumulated labelled dataset, not any single
session, and labels accrue at one per five minutes — about 288 a day. So this
runs under `systemd`, `tmux`, or `nohup`, and you check on it later.

```bash
python3 daemon.py --data-dir ../data                  # run until stopped
python3 daemon.py --data-dir ../data --duration 86400 # one day
python3 daemon.py --no-ticks                          # rounds only
python3 daemon.py --no-wait                           # skip the boundary wait
python3 daemon.py --status --data-dir ../data         # summarise and exit
```

By default the daemon **idles until the next 5-minute window boundary** before
recording. Starting mid-window guarantees the first round's open is wrong,
because we never saw the real one — so waiting costs at most five idle minutes
and buys a clean first label. Pass `--no-wait` to start immediately; that round
will be flagged `late_open` and excluded from training.

Example log:

```
[2026-09-27 15:54:30] starting: symbol=btcusdt data=../data ticks=True
[2026-09-27 15:54:32] clock offset +8.4ms (rtt 168ms, 5 samples)
[2026-09-27 15:55:00] round btc-updown-5m-1790488200 -> DOWN     -0.70bps  66 ticks
[2026-09-27 16:00:00] heartbeat: 84540.10  window btc-updown-5m-1790488500  210s left  18 msg/s
```

`--status` is the one to run when you come back:

```
rounds recorded : 3
  usable        : 2
    up          : 0
    down        : 2
  excluded      : 1  (1 partial)
  change bps    : min -8.13  max -4.41
  first         : btc-updown-5m-1790490300
  last          : btc-updown-5m-1790490600

last 10 rounds:
  slug                         outcome        bps   ticks
  btc-updown-5m-1790490300     down         -4.41     895
  btc-updown-5m-1790490600     down         -8.13    1253
  btc-updown-5m-1790490900     up           +1.70     268  [excluded]

  [excluded] rounds are missing from the up/down tally above
```

Note that the up/down tally only counts **usable** rounds. An excluded round's
outcome is not a real label, and quietly folding it into a win rate is the easiest
way to build a strategy on numbers that do not mean anything.

## Graceful shutdown

`SIGINT`/`SIGTERM` finish the in-flight window before exiting, and mark it
`partial` so it never reaches the training set. A hard `SIGKILL` can still
truncate mid-write, which is why every line is flushed on write and
`load_rounds` skips an unparseable final line.

## Keeping it alive

```bash
# tmux
tmux new -d -s feed './daemon.py --data-dir ../data'

# systemd
[Unit]
Description=btc feed recorder
[Service]
WorkingDirectory=/home/adam/AI/consensus-bot/3-headless
ExecStart=/usr/bin/python3 daemon.py --data-dir ../data
Restart=always
RestartSec=10
[Install]
WantedBy=multi-user.target
```

With `Restart=always`, expect a `late_open` round after every restart that lands
mid-window. That is correct behaviour, not noise — the `usable_for_training` flag
in the data already excludes it.

## Disk

Roughly 350 bytes per trade, and BTC prints around 30-60 trades a minute, so
about 8-15 MB per hour, or 200-400 MB per day of raw ticks. If that matters,
either run with `--no-ticks` (rounds only, a few KB/day) or prune old files:

```bash
find ../data -name 'ticks-*.jsonl' -mtime +7 -delete
```
