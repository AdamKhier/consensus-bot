# Consensus Bot — BTC 5-Minute Market Dataset Tooling

> Reconstructed 22 Nov 2026 from the repository itself, not from chat history.

## What it is
Tooling to build a labelled dataset of Polymarket's BTC Up/Down 5-minute markets, so strategies can be calibrated on real prices. It is **not** a trading bot — it captures and verifies prices.

Three correctness rules drive the design:
1. These markets resolve on Chainlink BTC/USD (newer ones a 60 s TWAP), not on Binance. Binance is a high-fidelity proxy, so a second venue runs in parallel to bound the error.
2. Windows are epoch-aligned (`start_epoch % 300 == 0`); ties resolve **Up**.
3. Clock offset is measured at startup — a 200 ms drift silently corrupts every open price.

## Stack
Python 3.11+ standard library only. No dependencies.

## Layout
| Folder | Purpose |
| --- | --- |
| `1-minimal/` | Single-file proof of concept |
| `2-full/` | Feed engine, window tracker, recorder, dashboard, tests, verifier |
| `3-headless/` | 24/7 daemon over the same engine |

## How to run
```bash
python3 1-minimal/price.py --count 10
cd 2-full && python3 -m unittest discover -s tests
cd 2-full && python3 -m feed.cli
cd 3-headless && python3 daemon.py --data-dir ../data --duration 86400
cd 2-full && python3 verify.py --data-dir ../data
```

## Status and gaps
- Data capture ran on 27 Sep 2026 only (`data/ticks-2026-09-27.jsonl`); collection has stopped.
- Polymarket's API is unreachable from this machine, so there is no market discovery, order book or execution.
- Leverage is unresolved: these are $1-payout binary markets, so position sizing is the only lever.

## Git
`AdamKhier/consensus-bot` — public.
