"""Bounded in-memory terminal replay buffer."""
from __future__ import annotations

from collections import deque


class ReplayBuffer:
    """Byte-preserving bounded FIFO for terminal output.

    Keeps only the newest ``capacity_bytes``; snapshots return a copy.
    """

    def __init__(self, capacity_bytes: int = 512 * 1024) -> None:
        if capacity_bytes <= 0:
            raise ValueError(f"Invalid replay capacity: {capacity_bytes!r}; expected > 0")
        self._capacity = int(capacity_bytes)
        self._chunks: deque[bytes] = deque()
        self._size = 0

    @property
    def capacity_bytes(self) -> int:
        return self._capacity

    @property
    def size_bytes(self) -> int:
        return self._size

    def append(self, data: bytes) -> None:
        if not data:
            return
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError(f"ReplayBuffer.append expects bytes, got {type(data).__name__}")
        chunk = bytes(data)
        if len(chunk) >= self._capacity:
            self._chunks.clear()
            self._chunks.append(chunk[-self._capacity :])
            self._size = self._capacity
            return
        self._chunks.append(chunk)
        self._size += len(chunk)
        overflow = self._size - self._capacity
        while overflow > 0 and self._chunks:
            oldest = self._chunks[0]
            if len(oldest) <= overflow:
                self._chunks.popleft()
                self._size -= len(oldest)
                overflow -= len(oldest)
            else:
                self._chunks[0] = oldest[overflow:]
                self._size -= overflow
                overflow = 0

    def snapshot(self, limit_bytes: int | None = None) -> bytes:
        if not self._chunks:
            return b""
        if limit_bytes is None:
            return b"".join(self._chunks)
        if limit_bytes <= 0:
            return b""
        want = min(int(limit_bytes), self._size)
        if want >= self._size:
            return b"".join(self._chunks)
        # Walk from newest to collect only the needed suffix.
        parts: list[bytes] = []
        remaining = want
        for chunk in reversed(self._chunks):
            if remaining <= 0:
                break
            if len(chunk) >= remaining:
                parts.append(chunk[-remaining:])
                remaining = 0
            else:
                parts.append(chunk)
                remaining -= len(chunk)
        parts.reverse()
        return b"".join(parts)
