"""In-memory state shared by the request threads: the duplicate guard and per-symbol locks."""

from __future__ import annotations

import threading
from collections.abc import Callable


class DedupeCache:
    """Remembers request bodies so a resent alert never places a second order.

    An entry is created by reserve() and never expires while its request is running. finish(keep=True)
    keeps it for `window_seconds` counted from the call; finish(keep=False) removes it at once.
    A window of 0 turns the guard off.
    """

    def __init__(self, window_seconds: int, clock: Callable[[], float]) -> None:
        self._window = window_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[str, float | None] = {}  # None = still running, else expiry time

    def _prune(self, now: float) -> None:
        expired = [key for key, expiry in self._entries.items() if expiry is not None and expiry <= now]
        for key in expired:
            del self._entries[key]

    def reserve(self, key: str) -> bool:
        """True when the caller owns the key; False when an identical request is running or recent."""
        if self._window <= 0:
            return True
        with self._lock:
            self._prune(self._clock())
            if key in self._entries:
                return False
            self._entries[key] = None
            return True

    def finish(self, key: str, keep: bool) -> None:
        if self._window <= 0:
            return
        with self._lock:
            now = self._clock()
            self._prune(now)
            if key not in self._entries:
                return
            if keep:
                self._entries[key] = now + self._window
            else:
                del self._entries[key]


class SymbolLocks:
    """One lock per (broker, symbol) so a position check and its order never interleave."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[tuple[str, str], threading.Lock] = {}

    def get(self, broker: str, symbol: str) -> threading.Lock:
        with self._guard:
            lock = self._locks.get((broker, symbol))
            if lock is None:
                lock = self._locks[(broker, symbol)] = threading.Lock()
            return lock
