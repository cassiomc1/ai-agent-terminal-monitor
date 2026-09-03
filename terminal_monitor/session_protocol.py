"""Bounded authenticated Unix-socket message framing for managed sessions."""
from __future__ import annotations

import json
import socket
from typing import Any

PROTOCOL_VERSION = 1
MAX_CONTROL_MESSAGE_BYTES = 64 * 1024


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


def receive_message(sock: socket.socket) -> dict[str, Any]:
    # Single-byte reads avoid over-reading into the next framed message and
    # never call unbounded makefile().readline(). Control traffic is low
    # frequency so this stays cheap; the 64 KiB bound caps syscall count.
    buf = bytearray()
    while True:
        try:
            chunk = sock.recv(1)
        except OSError as exc:
            raise SessionProtocolError(f"failed to receive control message: {exc}") from exc
        if not chunk:
            raise SessionProtocolError("peer closed connection before end of message")
        buf += chunk
        if len(buf) > MAX_CONTROL_MESSAGE_BYTES + 1:
            raise SessionProtocolError(f"control message exceeds {MAX_CONTROL_MESSAGE_BYTES} bytes")
        if chunk == b"\n":
            break
    try:
        text = bytes(buf).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SessionProtocolError(f"control message is not valid UTF-8: {exc}") from exc
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SessionProtocolError(f"control message is not valid JSON: {exc}") from exc
    if not isinstance(decoded, dict):
        raise SessionProtocolError("control message payload must be a JSON object")
    return decoded
