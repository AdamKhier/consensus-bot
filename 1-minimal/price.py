#!/usr/bin/env python3
"""Minimal live BTC price viewer - proof of concept, no dependencies.

Deliberately a single self-contained file so you can see the whole idea in one
screen: open a WebSocket to Binance, read BTCUSDT trades, print them. This is
the "is the data real and free?" test. Everything else in the project is a
productionised version of this.

    python3 price.py              # follow btcusdt
    python3 price.py --symbol ethusdt
    python3 price.py --seconds 20 # exit after 20s
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import ssl
import struct
import sys
import time

HOST = "stream.binance.com"
PORT = 9443


def open_stream(symbol: str) -> socket.socket:
    """Perform the WebSocket handshake and return a ready socket."""
    raw = socket.create_connection((HOST, PORT), timeout=15)
    sock = ssl.create_default_context().wrap_socket(raw, server_hostname=HOST)
    key = base64.b64encode(os.urandom(16)).decode()
    sock.sendall(
        (
            f"GET /ws/{symbol}@aggTrade HTTP/1.1\r\n"
            f"Host: {HOST}:{PORT}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        ).encode()
    )

    header = b""
    while b"\r\n\r\n" not in header:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("server closed during handshake")
        header += chunk
    status = header.split(b"\r\n", 1)[0].decode()
    if "101" not in status:
        raise ConnectionError(f"handshake failed: {status}")
    return sock


def read_exactly(sock: socket.socket, count: int) -> bytes:
    out = b""
    while len(out) < count:
        chunk = sock.recv(count - len(out))
        if not chunk:
            raise ConnectionError("stream closed")
        out += chunk
    return out


def read_message(sock: socket.socket) -> str:
    """Read one frame, skipping control frames. No extensions were negotiated."""
    while True:
        header = read_exactly(sock, 2)
        opcode = header[0] & 0x0F
        length = header[1] & 0x7F
        if length == 126:
            (length,) = struct.unpack(">H", read_exactly(sock, 2))
        elif length == 127:
            (length,) = struct.unpack(">Q", read_exactly(sock, 8))
        payload = read_exactly(sock, length) if length else b""
        if opcode == 0x8:
            raise ConnectionError("server sent close")
        if opcode == 0x9:  # ping -> pong
            sock.sendall(bytes([0x8A, 0x80]) + os.urandom(4))
            continue
        if opcode == 0xA:
            continue
        if opcode == 0x1:
            return payload.decode("utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Minimal BTC price viewer")
    parser.add_argument("--symbol", default="btcusdt")
    parser.add_argument("--seconds", type=float, default=0, help="exit after N seconds")
    parser.add_argument("--count", type=int, default=0, help="exit after N trades")
    args = parser.parse_args(argv)

    print(f"connecting to binance {args.symbol} ...", file=sys.stderr)
    sock = open_stream(args.symbol)
    print(f"streaming (ctrl-c to stop)\n", file=sys.stderr)

    started = time.time()
    trades = 0
    try:
        while True:
            message = read_message(sock)
            try:
                data = json.loads(message)
            except json.JSONDecodeError:
                continue
            if data.get("e") != "aggTrade":
                continue

            trades += 1
            price = float(data["p"])
            qty = float(data["q"])
            aggressor = "sell" if data.get("m") else "buy"
            latency = int(time.time() * 1000) - int(data["T"])
            stamp = time.strftime("%H:%M:%S", time.localtime(data["T"] / 1000))
            print(
                f"{stamp}  {price:>12,.2f}  {qty:>10.5f}  {aggressor:<4}"
                f"  latency {latency:>4}ms  #{trades}"
            )

            if args.count and trades >= args.count:
                break
            if args.seconds and time.time() - started >= args.seconds:
                break
    except KeyboardInterrupt:
        pass
    except ConnectionError as exc:
        print(f"\ndisconnected: {exc}", file=sys.stderr)
        return 1
    finally:
        sock.close()

    print(f"\n{trades} trades in {time.time() - started:.1f}s", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
