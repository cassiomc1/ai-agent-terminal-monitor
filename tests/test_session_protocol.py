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


if __name__ == "__main__":
    unittest.main()
