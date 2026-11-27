"""Shared process-wide lock helpers for routes that mutate model lifecycle."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Any, Iterator

from local_llm.preflight import ResourceError


@contextmanager
def locked(lock: Any, timeout: float = 600) -> Iterator[None]:
    if not lock.acquire(timeout=timeout):
        raise ResourceError("server is busy")
    try:
        yield
    finally:
        lock.release()


class AliasDownloadLocks:
    """Stable per-alias download locks; the guard only protects the mapping."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}

    def lock_for(self, alias: str) -> threading.Lock:
        with self._guard:
            lock = self._locks.get(alias)
            if lock is None:
                lock = threading.Lock()
                self._locks[alias] = lock
            return lock


__all__ = ["AliasDownloadLocks", "locked"]
