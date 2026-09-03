"""Bounded authenticated Unix-socket message framing for managed sessions."""
from __future__ import annotations

import json
import socket
import threading
import weakref
from typing import Any, ClassVar

PROTOCOL_VERSION = 1
MAX_CONTROL_MESSAGE_BYTES = 64 * 1024

# Snapshot transfers are chunked so full replay capacity fits through bounded
# frames. Worst case per frame: 32768 raw bytes -> ceil(32768/3)*4 = 43692
# Base64 chars + ~80 bytes of JSON envelope, well under the 64 KiB limit.
SNAPSHOT_CHUNK_BYTES = 32 * 1024

# recv() block size for the framed reader. Large enough to be cheap, small
# enough to stay far below any buffer concern.
_FRAME_READ_BYTES = 64 * 1024


class SessionProtocolError(ValueError):
    """Raised when a managed-session control message is invalid or too large."""


def encode_message(payload: dict[str, Any]) -> bytes:
    if not isinstance(payload, dict):
        raise SessionProtocolError("control message payload must be a JSON object")
    try:
        encoded = (json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise SessionProtocolError(f"control message is not JSON-serializable: {exc}") from exc
    if len(encoded) > MAX_CONTROL_MESSAGE_BYTES + 1:
        raise SessionProtocolError(f"control message exceeds {MAX_CONTROL_MESSAGE_BYTES} bytes")
    return encoded


def send_message(sock: socket.socket, payload: dict[str, Any]) -> None:
    data = encode_message(payload)
    try:
        sock.sendall(data)
    except OSError as exc:
        raise SessionProtocolError(f"failed to send control message: {exc}") from exc


def _decode_frame(raw: bytes) -> dict[str, Any]:
    try:
        text = bytes(raw).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SessionProtocolError(f"control message is not valid UTF-8: {exc}") from exc
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SessionProtocolError(f"control message is not valid JSON: {exc}") from exc
    if not isinstance(decoded, dict):
        raise SessionProtocolError("control message payload must be a JSON object")
    return decoded


class FramedReader:
    """Buffered newline-delimited JSON reader over a blocking socket.

    Reads in large blocks but splits frames internally, so sequential
    messages survive any packetization: several frames in one packet, one
    frame split across many packets, or any mix. Never performs an
    unbounded read: a frame without a newline past the size budget fails
    closed, and leftover bytes stay buffered for the next call.

    Readers on the same socket share one buffer (keyed weakly, so closed
    sockets never leak), keeping back-to-back ``receive_message`` calls
    composable.
    """

    _buffers: ClassVar[weakref.WeakKeyDictionary[socket.socket, bytearray]] = weakref.WeakKeyDictionary()
    _buffers_lock: ClassVar[threading.Lock] = threading.Lock()

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        with FramedReader._buffers_lock:
            buf = FramedReader._buffers.get(sock)
            if buf is None:
                buf = bytearray()
                FramedReader._buffers[sock] = buf
        self._buf = buf

    def read_message(self) -> dict[str, Any]:
        while True:
            newline = self._buf.find(b"\n")
            if newline >= 0:
                if newline > MAX_CONTROL_MESSAGE_BYTES:
                    raise SessionProtocolError(f"control message exceeds {MAX_CONTROL_MESSAGE_BYTES} bytes")
                raw = bytes(self._buf[: newline + 1])
                del self._buf[: newline + 1]
                return _decode_frame(raw)
            if len(self._buf) > MAX_CONTROL_MESSAGE_BYTES:
                raise SessionProtocolError(f"control message exceeds {MAX_CONTROL_MESSAGE_BYTES} bytes")
            try:
                chunk = self._sock.recv(_FRAME_READ_BYTES)
            except OSError as exc:
                raise SessionProtocolError(f"failed to receive control message: {exc}") from exc
            if not chunk:
                raise SessionProtocolError("peer closed connection before end of message")
            self._buf += chunk


def receive_message(sock: socket.socket) -> dict[str, Any]:
    """Read one framed message (convenience for single-message connections)."""
    return FramedReader(sock).read_message()
