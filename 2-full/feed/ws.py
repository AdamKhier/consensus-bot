"""Minimal RFC 6455 WebSocket client, stdlib only.

Only implements what a market-data subscription needs: a client-side handshake,
text-frame decoding, automatic pong replies, and transparent reconnection.
Deliberately no extensions (permessage-deflate is never negotiated) and no
outbound messages beyond ping/pong/close, so the attack surface stays small.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import ssl
import struct
import time
from dataclasses import dataclass, field
from typing import Iterator
from urllib.parse import urlparse

OP_CONT = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

_MAX_PAYLOAD = 8 * 1024 * 1024


class WebSocketError(Exception):
    pass


class ConnectionClosed(WebSocketError):
    """The peer hung up cleanly. Not a fault, just the end of the stream."""


@dataclass
class Stats:
    """Connection health counters, read by the CLI to show feed liveness."""

    connects: int = 0
    disconnects: int = 0
    messages: int = 0
    pings_sent: int = 0
    pongs_received: int = 0
    bytes_received: int = 0
    last_message_at: float = 0.0
    connected_at: float = 0.0
    reconnects: int = 0
    last_error: str = ""


def _mask(payload: bytes, key: bytes) -> bytes:
    """Apply the RFC 6455 client-to-server mask."""
    if not payload:
        return b""
    stream = (key * (len(payload) // 4 + 1))[: len(payload)]
    return bytes(a ^ b for a, b in zip(payload, stream))


def encode_frame(opcode: int, payload: bytes) -> bytes:
    """Encode a single masked client frame."""
    length = len(payload)
    header = bytearray([0x80 | opcode])
    if length < 126:
        header.append(0x80 | length)
    elif length < 65536:
        header.append(0x80 | 126)
        header += struct.pack(">H", length)
    else:
        header.append(0x80 | 127)
        header += struct.pack(">Q", length)
    # The 4-byte masking key is part of the frame and is always present on a
    # client frame, including when the payload is empty.
    mask_key = os.urandom(4)
    return bytes(header) + mask_key + _mask(payload, mask_key)


class WebSocketConnection:
    """A single connection to a WebSocket endpoint."""

    def __init__(
        self,
        url: str,
        timeout: float = 20.0,
        stats: Stats | None = None,
    ) -> None:
        self.url = url
        self.timeout = timeout
        # A caller-supplied Stats object is shared across reconnects, so counters
        # accumulate over the process lifetime rather than resetting each drop.
        self.stats = stats if stats is not None else Stats()
        self._sock: socket.socket | None = None
        self._buf = b""
        self._closed = False
        self._handshake(url)

    # -- connection setup -------------------------------------------------

    def _handshake(self, url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in ("ws", "wss"):
            raise WebSocketError(f"unsupported scheme: {parsed.scheme}")
        host = parsed.hostname
        if not host:
            raise WebSocketError(f"no host in url: {url}")
        port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"

        raw = socket.create_connection((host, port), timeout=self.timeout)
        try:
            if parsed.scheme == "wss":
                context = ssl.create_default_context()
                raw = context.wrap_socket(raw, server_hostname=host)
            raw.settimeout(self.timeout)
            raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

            key = base64.b64encode(os.urandom(16)).decode()
            request = (
                f"GET {path} HTTP/1.1\r\n"
                f"Host: {host}:{port}\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n"
                "\r\n"
            )
            raw.sendall(request.encode())

            header_blob = b""
            while b"\r\n\r\n" not in header_blob:
                chunk = raw.recv(4096)
                if not chunk:
                    raise WebSocketError("connection closed during handshake")
                header_blob += chunk
                if len(header_blob) > 65536:
                    raise WebSocketError("handshake headers too large")

            head, _, rest = header_blob.partition(b"\r\n\r\n")
            status_line = head.split(b"\r\n", 1)[0].decode("latin-1")
            if "101" not in status_line:
                raise WebSocketError(f"handshake rejected: {status_line}")
            self._buf = rest
        except Exception:
            raw.close()
            raise

        self._sock = raw
        self.stats.connects += 1
        self.stats.connected_at = time.time()

    # -- frame decoding ---------------------------------------------------

    def _recv_exactly(self, count: int) -> bytes:
        while len(self._buf) < count:
            assert self._sock is not None
            chunk = self._sock.recv(max(4096, count - len(self._buf)))
            if not chunk:
                raise ConnectionClosed("connection closed by peer")
            self._buf += chunk
        out, self._buf = self._buf[:count], self._buf[count:]
        return out

    def _read_frame(self) -> tuple[bool, int, bytes]:
        """Return (fin, opcode, payload) for one frame, handling control frames."""
        while True:
            header = self._recv_exactly(2)
            fin = bool(header[0] & 0x80)
            opcode = header[0] & 0x0F
            masked = bool(header[1] & 0x80)
            length = header[1] & 0x7F

            if length == 126:
                (length,) = struct.unpack(">H", self._recv_exactly(2))
            elif length == 127:
                (length,) = struct.unpack(">Q", self._recv_exactly(8))
            if length > _MAX_PAYLOAD:
                raise WebSocketError(f"payload too large: {length}")

            mask = self._recv_exactly(4) if masked else b""
            payload = self._recv_exactly(length) if length else b""
            if masked and payload:
                payload = _mask(payload, mask)

            self.stats.bytes_received += len(payload)

            # Control frames may be interleaved between fragments; handle inline.
            if opcode == OP_PING:
                self._send(OP_PONG, payload)
                continue
            if opcode == OP_PONG:
                self.stats.pongs_received += 1
                continue
            if opcode == OP_CLOSE:
                self._closed = True
                raise ConnectionClosed("peer sent close frame")
            return fin, opcode, payload

    def _send(self, opcode: int, payload: bytes) -> None:
        if self._sock is None:
            raise WebSocketError("not connected")
        self._sock.sendall(encode_frame(opcode, payload))
        if opcode == OP_PING:
            self.stats.pings_sent += 1

    def ping(self) -> None:
        self._send(OP_PING, b"ping")

    def send_text(self, text: str) -> None:
        """Send a text frame. Used for protocol-level subscribes."""
        self._send(OP_TEXT, text.encode())

    # -- public iteration -------------------------------------------------

    def messages(self) -> Iterator[str]:
        """Yield decoded text messages until the connection drops."""
        while not self._closed:
            fin, opcode, payload = self._read_frame()

            if opcode == OP_CONT:
                raise WebSocketError("continuation frame with nothing to continue")

            if not fin:
                # Fragmented text message: accumulate until FIN.
                chunks = [payload]
                while True:
                    cfin, cop, cpayload = self._read_frame()
                    if cop != OP_CONT:
                        raise WebSocketError("fragment interleaved with data frame")
                    chunks.append(cpayload)
                    if cfin:
                        break
                payload = b"".join(chunks)

            if opcode == OP_BINARY:
                continue

            self.stats.messages += 1
            self.stats.last_message_at = time.time()
            yield payload.decode("utf-8", errors="replace")

    def close(self) -> None:
        self._closed = True
        if self._sock is not None:
            try:
                self._send(OP_CLOSE, b"")
            except OSError:
                pass
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        self.stats.disconnects += 1


def stream(
    url: str,
    *,
    on_connect=None,
    reconnect: bool = True,
    base_delay: float = 0.5,
    max_delay: float = 30.0,
    timeout: float = 20.0,
    ping_interval: float = 20.0,
    stats: Stats | None = None,
) -> Iterator[str]:
    """Yield messages from `url`, reconnecting forever on failure.

    `on_connect(connection)` is invoked after each successful handshake, which is
    where callers subscribe or resync state. Pass `stats` to accumulate connection
    health across every reconnect.
    """
    if stats is None:
        stats = Stats()
    delay = base_delay
    while True:
        connection: WebSocketConnection | None = None
        try:
            connection = WebSocketConnection(url, timeout=timeout, stats=stats)
            delay = base_delay
            # `connects` counts every successful handshake; `reconnects` counts
            # only the ones that followed a drop, which is what you actually
            # want to alert on.
            if stats.connects > 1:
                stats.reconnects += 1
            if on_connect is not None:
                on_connect(connection)

            last_ping = time.time()
            for message in connection.messages():
                now = time.time()
                if now - last_ping >= ping_interval:
                    try:
                        connection.ping()
                    except OSError:
                        break
                    last_ping = now
                yield message
        except ConnectionClosed as exc:
            # A clean hangup is the end of the stream, not a failure. With
            # reconnect enabled we just dial again; otherwise iteration ends.
            stats.last_error = str(exc)
            if not reconnect:
                return
        except OSError as exc:
            if not reconnect:
                raise
            stats.last_error = f"{type(exc).__name__}: {exc}"
        except GeneratorExit:
            raise
        except WebSocketError as exc:
            # Protocol-level fault: too big, bad framing, unmaskable.
            if not reconnect:
                raise
            stats.last_error = f"{type(exc).__name__}: {exc}"
        finally:
            # Also covers GeneratorExit and normal completion, so the socket is
            # never leaked if the consumer abandons the generator.
            if connection is not None:
                connection.close()

        if not reconnect:
            return
        time.sleep(delay)
        delay = min(delay * 2, max_delay)


def parse_json(message: str) -> dict:
    try:
        return json.loads(message)
    except json.JSONDecodeError:
        return {}
