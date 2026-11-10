"""WebSocket frame encoding/decoding tests (stdlib unittest)."""

from __future__ import annotations

import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from feed import ws  # noqa: E402


def server_frame(opcode: int, payload: bytes, fin: bool = True) -> bytes:
    """Build an unmasked server-to-client frame."""
    header = bytearray([(0x80 if fin else 0) | opcode])
    length = len(payload)
    if length < 126:
        header.append(length)
    elif length < 65536:
        header.append(126)
        header += struct.pack(">H", length)
    else:
        header.append(127)
        header += struct.pack(">Q", length)
    return bytes(header) + payload


class FakeSocket:
    """Feeds a scripted byte stream to the frame decoder."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0
        self.sent = b""

    def recv(self, count: int) -> bytes:
        chunk = self._data[self._pos : self._pos + count]
        self._pos += len(chunk)
        return chunk

    def sendall(self, data: bytes) -> None:
        self.sent += data

    def settimeout(self, _timeout) -> None:
        pass

    def setsockopt(self, *_args) -> None:
        pass

    def close(self) -> None:
        pass


def decoder(data: bytes) -> ws.WebSocketConnection:
    conn = ws.WebSocketConnection.__new__(ws.WebSocketConnection)
    conn.timeout = 1.0
    conn.stats = ws.Stats()
    conn._sock = FakeSocket(data)
    conn._buf = b""
    conn._closed = False
    return conn


def unmask_client_frame(frame: bytes) -> bytes:
    """Recover the payload of a masked client frame."""
    length = frame[1] & 0x7F
    header_len = 2
    if length == 126:
        header_len = 4
    elif length == 127:
        header_len = 10
    key = frame[header_len : header_len + 4]
    payload = frame[header_len + 4 :]
    return bytes(b ^ key[i % 4] for i, b in enumerate(payload))


def drain(connection) -> list[str]:
    """Collect messages until the scripted stream is exhausted.

    A real socket raises when the peer goes away, so a clean end of stream is
    surfaced as WebSocketError; the reconnect wrapper is what normally catches it.
    """
    out = []
    iterator = connection.messages()
    while True:
        try:
            out.append(next(iterator))
        except StopIteration:
            break
        except ws.WebSocketError:
            break
    return out


class TestEncodeFrame(unittest.TestCase):
    def test_short_payload_is_masked(self):
        frame = ws.encode_frame(ws.OP_TEXT, b"hello")
        self.assertEqual(frame[0] & 0x0F, ws.OP_TEXT)
        self.assertEqual(frame[0] & 0x80, 0x80, "FIN bit must be set")
        self.assertTrue(frame[1] & 0x80, "client frames must be masked")
        self.assertEqual(frame[1] & 0x7F, 5)
        self.assertEqual(unmask_client_frame(frame), b"hello")

    def test_medium_payload_uses_126_length(self):
        payload = b"x" * 300
        frame = ws.encode_frame(ws.OP_TEXT, payload)
        self.assertEqual(frame[1] & 0x7F, 126)
        self.assertEqual(struct.unpack(">H", frame[2:4])[0], 300)
        self.assertEqual(unmask_client_frame(frame), payload)

    def test_long_payload_uses_127_length(self):
        payload = b"x" * 70000
        frame = ws.encode_frame(ws.OP_TEXT, payload)
        self.assertEqual(frame[1] & 0x7F, 127)
        self.assertEqual(struct.unpack(">Q", frame[2:10])[0], 70000)
        self.assertEqual(unmask_client_frame(frame), payload)

    def test_empty_payload_still_carries_mask_key(self):
        # RFC 6455 requires the 4-byte key on every client frame, even with no
        # payload, so the frame is 2 header + 4 key = 6 bytes.
        frame = ws.encode_frame(ws.OP_PING, b"")
        self.assertEqual(len(frame), 6)
        self.assertEqual(unmask_client_frame(frame), b"")


class TestDecode(unittest.TestCase):
    def test_short_text_frame(self):
        conn = decoder(server_frame(ws.OP_TEXT, b'{"e":"trade"}'))
        fin, opcode, payload = conn._read_frame()
        self.assertTrue(fin)
        self.assertEqual(opcode, ws.OP_TEXT)
        self.assertEqual(payload, b'{"e":"trade"}')

    def test_126_length_frame(self):
        conn = decoder(server_frame(ws.OP_TEXT, b"y" * 1000))
        _, opcode, payload = conn._read_frame()
        self.assertEqual(opcode, ws.OP_TEXT)
        self.assertEqual(payload, b"y" * 1000)

    def test_127_length_frame(self):
        conn = decoder(server_frame(ws.OP_TEXT, b"z" * 70000))
        _, _, payload = conn._read_frame()
        self.assertEqual(payload, b"z" * 70000)

    def test_ping_is_answered_with_pong(self):
        conn = decoder(server_frame(ws.OP_PING, b"hi") + server_frame(ws.OP_TEXT, b"next"))
        fin, opcode, payload = conn._read_frame()
        self.assertEqual((fin, opcode, payload), (True, ws.OP_TEXT, b"next"))
        sent = conn._sock.sent
        self.assertEqual(sent[0] & 0x0F, ws.OP_PONG)
        self.assertEqual(unmask_client_frame(sent), b"hi")
        self.assertEqual(conn.stats.pings_sent, 0)

    def test_pong_is_counted_not_yielded(self):
        conn = decoder(server_frame(ws.OP_PONG, b"") + server_frame(ws.OP_TEXT, b"data"))
        _, opcode, _ = conn._read_frame()
        self.assertEqual(opcode, ws.OP_TEXT)
        self.assertEqual(conn.stats.pongs_received, 1)

    def test_masked_frame_is_unmasked(self):
        payload = b"masked payload"
        mask = b"\x01\x02\x03\x04"
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        frame = bytearray(server_frame(ws.OP_TEXT, masked))
        frame[1] |= 0x80
        frame = bytes(frame[:2]) + mask + masked
        conn = decoder(frame)
        _, _, out = conn._read_frame()
        self.assertEqual(out, payload)

    def test_close_frame_raises(self):
        conn = decoder(server_frame(ws.OP_CLOSE, b""))
        with self.assertRaises(ws.WebSocketError):
            conn._read_frame()

    def test_truncated_stream_raises(self):
        conn = decoder(b"\x81\x7f\x00\x00")
        with self.assertRaises(ws.WebSocketError):
            conn._read_frame()


class TestMessageIterator(unittest.TestCase):
    def test_fragmented_message_is_reassembled(self):
        # Only the final fragment carries FIN; earlier ones must not, or the
        # reassembler stops early and truncates the message.
        stream = (
            server_frame(ws.OP_TEXT, b'{"a":', fin=False)
            + server_frame(ws.OP_CONT, b"1,", fin=False)
            + server_frame(ws.OP_CONT, b"2}", fin=True)
        )
        conn = decoder(stream)
        self.assertEqual(drain(conn), ['{"a":1,2}'])

    def test_three_fragments(self):
        stream = (
            server_frame(ws.OP_TEXT, b"a", fin=False)
            + server_frame(ws.OP_CONT, b"b", fin=False)
            + server_frame(ws.OP_CONT, b"c", fin=True)
        )
        self.assertEqual(drain(decoder(stream)), ["abc"])

    def test_control_frame_between_fragments_is_allowed(self):
        stream = (
            server_frame(ws.OP_TEXT, b"a", fin=False)
            + server_frame(ws.OP_PING, b"")
            + server_frame(ws.OP_CONT, b"b", fin=True)
        )
        self.assertEqual(drain(decoder(stream)), ["ab"])

    def test_multiple_messages_in_order(self):
        stream = server_frame(ws.OP_TEXT, b"one") + server_frame(ws.OP_TEXT, b"two")
        self.assertEqual(drain(decoder(stream)), ["one", "two"])

    def test_binary_frames_are_skipped(self):
        stream = server_frame(ws.OP_BINARY, b"\x00\x01") + server_frame(ws.OP_TEXT, b"kept")
        self.assertEqual(drain(decoder(stream)), ["kept"])


class TestMaskHelper(unittest.TestCase):
    def test_mask_is_self_inverse(self):
        key = b"\xde\xad\xbe\xef"
        payload = os.urandom(1000)
        self.assertEqual(ws._mask(ws._mask(payload, key), key), payload)

    def test_empty_mask_roundtrip(self):
        self.assertEqual(ws._mask(b"", b"\x01\x02\x03\x04"), b"")


if __name__ == "__main__":
    unittest.main()
