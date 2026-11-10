# 1 - Minimal price viewer

Single file, no dependencies, no framework. The point is to answer one question
in ten seconds: **is live BTC data actually free and reachable from here?**

If this works, the whole project is viable. Everything else is a productionised
version of this file.

```bash
python3 price.py                        # follow btcusdt
python3 price.py --symbol ethusdt
python3 price.py --count 10             # exit after 10 trades
python3 price.py --seconds 20
```

Sample output:

```
15:47:43     84,533.29     0.00020  sell  latency  150ms  #1
15:47:44     84,533.30     0.77012  buy   latency  150ms  #2
15:47:46     84,533.30     0.00150  buy   latency  161ms  #3
```

## What it shows

- **`latency`** is the gap between Binance's trade timestamp and our receive
  time. ~150ms from this machine, dominated by network distance, not by our
  code. Worth knowing: it is the floor on how fast we could react to a move.
- **`sell`/`buy`** is the *aggressor* side (`m` flag on the aggTrade payload),
  i.e. who crossed the spread, not who ended up holding it.
- Prices cluster into sub-cent repeats because BTC was genuinely flat at the
  time. That is not a bug, and it is exactly the regime where a 5-minute
  up/down market is hardest to predict.

## Why it is written this way

No `pip` on this machine, and no `node`. So the WebSocket client is ~60 lines of
`socket` + `ssl` + `struct`: handshake, read the 2-byte header, decode a 7/16/64
bit length, read the payload. No extensions are negotiated, so every frame is
unmasked and there is nothing to decompress.

For the production version, see `../2-full/feed/ws.py` — same idea, plus
fragment reassembly, ping/pong, masking, and reconnection with backoff.

## What this does *not* do

No window tracking, no TWAP, no recording, no second venue, no reconnect. Use
`../2-full` for anything beyond "is the data live".
