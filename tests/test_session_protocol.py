import contextlib
import pathlib
import socket
import sys
import threading
import unittest

REPO_ROOT = pathlib.Path(__file__).parents[1]
sys.path.insert(0, str(REPO_ROOT))


from terminal_monitor.session_protocol import (  # noqa: E402
    MAX_CONTROL_MESSAGE_BYTES,
    FramedReader,
    SessionProtocolError,
    encode_message,
    receive_message,
    send_message,
)


class SessionProtocolTests(unittest.TestCase):
    def test_round_trip_single_object(self):
        s1, s2 = socket.socketpair()
        try:
            send_message(s1, {"version": 1, "op": "status", "token": "abc"})
            decoded = receive_message(s2)
            self.assertEqual(decoded, {"version": 1, "op": "status", "token": "abc"})
        finally:
            s1.close()
            s2.close()

    def test_multiple_sequential_messages(self):
        s1, s2 = socket.socketpair()
        try:
            send_message(s1, {"a": 1})
            send_message(s1, {"b": 2})
            self.assertEqual(receive_message(s2), {"a": 1})
            self.assertEqual(receive_message(s2), {"b": 2})
        finally:
            s1.close()
            s2.close()

    def test_oversized_payload_rejected_on_encode(self):
        with self.assertRaises(SessionProtocolError):
            encode_message({"data": "x" * (MAX_CONTROL_MESSAGE_BYTES + 1)})

    def test_oversized_incoming_rejected(self):
        s1, s2 = socket.socketpair()
        # Large payloads exceed socketpair buffers; send in the background so
        # sendall does not deadlock against a receiver that hasn't started yet.
        payload = b"{" + b"x" * (MAX_CONTROL_MESSAGE_BYTES + 10) + b"\n"

        def _send() -> None:
            with contextlib.suppress(OSError):
                s1.sendall(payload)

        sender = threading.Thread(target=_send, daemon=True)
        try:
            sender.start()
            with self.assertRaises(SessionProtocolError):
                receive_message(s2)
            sender.join(timeout=5.0)
        finally:
            s1.close()
            s2.close()

    def test_non_object_json_rejected(self):
        s1, s2 = socket.socketpair()
        try:
            s1.sendall(b"[1,2,3]\n")
            with self.assertRaises(SessionProtocolError):
                receive_message(s2)
        finally:
            s1.close()
            s2.close()

    def test_malformed_json_rejected(self):
        s1, s2 = socket.socketpair()
        try:
            s1.sendall(b"{not json\n")
            with self.assertRaises(SessionProtocolError):
                receive_message(s2)
        finally:
            s1.close()
            s2.close()

    def test_eof_without_newline_rejected(self):
        s1, s2 = socket.socketpair()
        try:
            s1.sendall(b'{"partial": true')
            s1.close()
            with self.assertRaises(SessionProtocolError):
                receive_message(s2)
        finally:
            with contextlib.suppress(OSError):
                s1.close()
            s2.close()

    def test_encode_rejects_non_dict(self):
        with self.assertRaises(SessionProtocolError):
            encode_message(["not", "a", "dict"])  # type: ignore[arg-type]


class FramedReaderTests(unittest.TestCase):
    def _pair(self):
        s1, s2 = socket.socketpair()
        self.addCleanup(s1.close)
        self.addCleanup(s2.close)
        return s1, s2

    def test_multiple_messages_in_one_packet(self):
        s1, s2 = self._pair()
        s1.sendall(encode_message({"n": 1}) + encode_message({"n": 2}) + encode_message({"n": 3}))
        reader = FramedReader(s2)
        self.assertEqual(reader.read_message(), {"n": 1})
        self.assertEqual(reader.read_message(), {"n": 2})
        self.assertEqual(reader.read_message(), {"n": 3})

    def test_message_split_across_packets(self):
        s1, s2 = self._pair()
        encoded = encode_message({"key": "value", "n": 42})
        for index in range(0, len(encoded), 3):
            s1.sendall(encoded[index : index + 3])
        s1.close()
        reader = FramedReader(s2)
        self.assertEqual(reader.read_message(), {"key": "value", "n": 42})

    def test_back_to_back_large_messages(self):
        s1, s2 = self._pair()
        big = "y" * 8000
        payload = encode_message({"data": big, "i": 1}) + encode_message({"data": big, "i": 2})

        def _send() -> None:
            with contextlib.suppress(OSError):
                s1.sendall(payload)

        sender = threading.Thread(target=_send, daemon=True)
        sender.start()
        reader = FramedReader(s2)
        first = reader.read_message()
        second = reader.read_message()
        sender.join(timeout=5.0)
        self.assertEqual((first["i"], second["i"]), (1, 2))
        self.assertEqual(first["data"], big)

    def test_oversized_message(self):
        s1, s2 = self._pair()

        def _send() -> None:
            with contextlib.suppress(OSError):
                s1.sendall(b"{" + b"z" * (MAX_CONTROL_MESSAGE_BYTES + 10) + b"\n")

        sender = threading.Thread(target=_send, daemon=True)
        sender.start()
        with self.assertRaises(SessionProtocolError):
            FramedReader(s2).read_message()
        sender.join(timeout=5.0)

    def test_eof_mid_message(self):
        s1, s2 = self._pair()
        s1.sendall(b'{"half": tru')
        s1.close()
        with self.assertRaises(SessionProtocolError):
            FramedReader(s2).read_message()


if __name__ == "__main__":
    unittest.main()
