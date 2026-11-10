"""Integration test for the reconnect loop, using a local throwaway server.

Reconnection is the code most likely to break in production and hardest to notice
when it does: the feed keeps "working", it just silently stops receiving. So it
gets a real socket and a real server that hangs up on cue.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import struct
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from feed import ws  # noqa: E402


def encode_server_frame(opcode: int, payload: bytes) -> bytes:
    """Unmasked server-to-client frame."""
    header = bytearray([0x80 | opcode])
    length = len(payload)
    if length < 126:
        header.append(length)
    else:
        header.append(126)
        header += struct.pack(">H", length)
    return bytes(header) + payload


WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def accept_key(client_key: str) -> str:
    import hashlib

    return base64.b64encode(hashlib.sha1((client_key + WS_GUID).encode()).digest()).decode()


class FakeServer:
    """Accepts one connection, completes the handshake, sends N messages, hangs up."""

    def __init__(self, messages: list[str]) -> None:
        self.messages = messages
        self.connections = 0
        self._listener = socket.socket()
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(5)
        self.port = self._listener.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._listener.accept()
            except OSError:
                return
            self.connections += 1
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(10)
            request = b""
            while b"\r\n\r\n" not in request:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                request += chunk

            key = ""
            for line in request.decode("latin-1").split("\r\n"):
                if line.lower().startswith("sec-websocket-key:"):
                    key = line.split(":", 1)[1].strip()
                    break
            conn.sendall(
                (
                    "HTTP/1.1 101 Switching Protocols\r\n"
                    "Upgrade: websocket\r\n"
                    "Connection: Upgrade\r\n"
                    f"Sec-WebSocket-Accept: {accept_key(key)}\r\n"
                    "\r\n"
                ).encode()
            )

            for message in self.messages:
                conn.sendall(encode_server_frame(ws.OP_TEXT, message.encode()))
                time.sleep(0.01)
            time.sleep(0.05)
        except OSError:
            pass
        finally:
            conn.close()

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/stream"

    def close(self) -> None:
        self._listener.close()


class TestReconnect(unittest.TestCase):
    def setUp(self):
        self.server = FakeServer([json.dumps({"n": i}) for i in range(3)])
        self.addCleanup(self.server.close)

    def test_streams_messages_from_a_live_server(self):
        received = []
        for message in ws.stream(self.server.url, reconnect=False, base_delay=0.01):
            received.append(json.loads(message))
        self.assertEqual(received, [{"n": 0}, {"n": 1}, {"n": 2}])
        self.assertEqual(self.server.connections, 1)

    def test_clean_hangup_ends_iteration_without_raising(self):
        # A server hangup with reconnect disabled is a normal end of stream.
        received = list(ws.stream(self.server.url, reconnect=False, base_delay=0.01))
        self.assertEqual(len(received), 3)

    def test_reconnects_after_server_hangs_up(self):
        """The whole point: a dropped feed must recover by itself."""
        received = []
        deadline = time.time() + 20
        for message in ws.stream(self.server.url, reconnect=True, base_delay=0.01):
            received.append(json.loads(message))
            # Three messages per connection; wait for two full rounds of them
            # rather than counting connections, which race with delivery.
            if len(received) >= 6 or time.time() > deadline:
                break
        self.assertGreaterEqual(
            self.server.connections, 2, "client should have reconnected after the hangup"
        )
        self.assertGreaterEqual(len(received), 6)
        # Messages restart from 0 on each new connection, proving it reconnected.
        self.assertEqual(received[3], {"n": 0})

    def test_stats_accumulate_across_reconnects(self):
        stats = ws.Stats()
        deadline = time.time() + 20
        for _message in ws.stream(
            self.server.url, reconnect=True, base_delay=0.01, stats=stats
        ):
            if stats.messages >= 6 or time.time() > deadline:
                break
        self.assertGreaterEqual(stats.connects, 2)
        self.assertGreaterEqual(stats.reconnects, 1)
        # reconnects counts only the dials that followed a drop.
        self.assertEqual(stats.reconnects, stats.connects - 1)
        self.assertGreaterEqual(stats.messages, 6)


class TestHandshakeRejection(unittest.TestCase):
    def test_non_101_response_raises(self):
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        self.addCleanup(listener.close)

        def serve():
            conn, _ = listener.accept()
            conn.recv(4096)
            conn.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
            conn.close()

        threading.Thread(target=serve, daemon=True).start()
        with self.assertRaises(ws.WebSocketError):
            ws.WebSocketConnection(f"ws://127.0.0.1:{port}/stream")

    def test_bad_scheme_rejected(self):
        with self.assertRaises(ws.WebSocketError):
            ws.WebSocketConnection("http://127.0.0.1:1/stream")

    def test_connection_refused_is_oserror(self):
        # Port 1 is reserved and nothing listens on it.
        with self.assertRaises(OSError):
            ws.WebSocketConnection("ws://127.0.0.1:1/stream", timeout=2)


class TestGeneratorCleanup(unittest.TestCase):
    def test_abandoning_the_generator_closes_the_socket(self):
        server = FakeServer([json.dumps({"n": i}) for i in range(100)])
        self.addCleanup(server.close)
        generator = ws.stream(server.url, reconnect=True, base_delay=0.01)
        next(generator)
        generator.close()
        # Closing the generator must run the finally block; a second close is a
        # no-op rather than an error.
        generator.close()


if __name__ == "__main__":
    unittest.main()
