"""Optional remote-viewing provider contract (read-only first release)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class RemoteShare:
    provider: str
    session_id: str
    share_url: str
    encrypted: bool
    read_only: bool


class RemoteProvider(Protocol):
    def available(self) -> tuple[bool, str]: ...
    def share_read_only(self, *, state_dir: str) -> RemoteShare: ...
    def stop(self, session_id: str) -> tuple[bool, str]: ...
