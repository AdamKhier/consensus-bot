"""Venue adapter parsing tests using captured-shape payloads (no network)."""

from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from feed import binance, coinbase  # noqa: E402
from feed.types import Side  # noqa: E402


def envelope(stream: str, data: dict) -> str:
    return json.dumps({"stream": stream, "data": data})


class TestBinanceParse(unittest.TestCase):
    def test_agg_trade_becomes_tick(self):
        message = envelope(
            "btcusdt@aggTrade",
            {"e": "aggTrade", "E": 1700000000123, "s": "BTCUSDT",
             "a": 1, "p": "84510.50", "q": "0.00275", "T": 1700000000119, "m": True},
        )
        tick, quote = binance.parse(message)
        self.assertIsNotNone(tick)
        self.assertEqual(tick.venue, "binance")
        self.assertEqual(tick.symbol, "BTCUSDT")
        self.assertEqual(tick.price, 84510.50)
        self.assertEqual(tick.qty, 0.00275)
        self.assertEqual(tick.event_ms, 1700000000119)
        self.assertEqual(tick.side, Side.SELL, "m=True means the buyer was passive")
        self.assertEqual(quote, {})

    def test_agg_trade_buyer_aggressor_is_buy(self):
        message = envelope(
            "btcusdt@aggTrade",
            {"e": "aggTrade", "p": "100.0", "q": "1.0", "T": 1, "m": False},
        )
        tick, _ = binance.parse(message)
        self.assertEqual(tick.side, Side.BUY)

    def test_book_ticker_becomes_quote(self):
        message = envelope(
            "btcusdt@bookTicker",
            {"e": "bookTicker", "u": 1, "b": "84509.99", "B": "5.0",
             "a": "84509.10", "A": "3.0", "T": 1700000000200, "E": 1700000000201},
        )
        tick, quote = binance.parse(message)
        self.assertIsNone(tick)
        self.assertEqual(quote["best_bid"], 84509.99)
        self.assertEqual(quote["best_ask"], 84509.10)
        self.assertEqual(quote["event_ms"], 1700000000201)

    def test_depth_becomes_quote(self):
        message = envelope(
            "btcusdt@depth5@100ms",
            {"lastUpdateId": 1, "bids": [["84509.99", "5.0"]], "asks": [["84510.50", "2.0"]]},
        )
        tick, quote = binance.parse(message)
        self.assertIsNone(tick)
        self.assertEqual(quote["best_bid"], 84509.99)
        self.assertEqual(quote["best_ask"], 84510.50)

    def test_malformed_trade_yields_nothing(self):
        message = envelope("btcusdt@aggTrade", {"e": "aggTrade", "p": "abc"})
        tick, quote = binance.parse(message)
        self.assertIsNone(tick)
        self.assertEqual(quote, {})

    def test_empty_depth_arrays_handled(self):
        message = envelope("btcusdt@depth5@100ms", {"bids": [], "asks": []})
        tick, quote = binance.parse(message)
        self.assertIsNone(tick)
        self.assertEqual(quote, {})

    def test_non_json_returns_empty(self):
        tick, quote = binance.parse("not json at all")
        self.assertIsNone(tick)
        self.assertEqual(quote, {})

    def test_stream_url_contains_all_three_streams(self):
        url = binance.stream_url("btcusdt")
        for stream in ("btcusdt@aggTrade", "btcusdt@bookTicker", "btcusdt@depth5@100ms"):
            self.assertIn(stream, url)
        self.assertTrue(url.startswith("wss://"))


class TestCoinbaseParse(unittest.TestCase):
    def test_market_trade_becomes_tick(self):
        message = json.dumps(
            {
                "channel": "market_trades",
                "events": [
                    {
                        "trades": [
                            {
                                "trade_id": "1",
                                "price": "84500.25",
                                "size": "0.001",
                                "side": "SELL",
                                "time": "2026-09-27T05:35:23.968Z",
                            }
                        ]
                    }
                ],
            }
        )
        tick, _ = coinbase.parse(message)
        self.assertIsNotNone(tick)
        self.assertEqual(tick.venue, "coinbase")
        self.assertEqual(tick.price, 84500.25)
        self.assertEqual(tick.side, Side.SELL)
        self.assertEqual(tick.symbol, "BTC-USD")

    def test_buy_side(self):
        message = json.dumps(
            {
                "channel": "market_trades",
                "events": [{"trades": [{"price": "1", "size": "1", "side": "BUY",
                                        "time": "2026-09-27T05:35:23.968Z"}]}],
            }
        )
        tick, _ = coinbase.parse(message)
        self.assertEqual(tick.side, Side.BUY)

    def test_ticker_becomes_quote(self):
        message = json.dumps(
            {
                "channel": "ticker",
                "events": [{"tickers": [{"best_bid": "84499.00", "best_ask": "84501.00"}]}],
            }
        )
        tick, quote = coinbase.parse(message)
        self.assertIsNone(tick)
        self.assertEqual(quote["best_bid"], 84499.00)
        self.assertEqual(quote["best_ask"], 84501.00)

    def test_subscription_ack_yields_nothing(self):
        tick, quote = coinbase.parse(json.dumps({"channel": "subscriptions", "events": []}))
        self.assertIsNone(tick)
        self.assertEqual(quote, {})

    def test_parse_time_handles_utc_zulu(self):
        self.assertEqual(coinbase.parse_time("2026-09-27T05:35:23.968Z"), 1790487323968)

    def test_parse_time_bad_input_does_not_crash(self):
        self.assertIsInstance(coinbase.parse_time("garbage"), int)

    def test_stream_url_includes_product(self):
        self.assertIn("BTC-USD", coinbase.stream_url())


if __name__ == "__main__":
    unittest.main()
