"""Shared process-wide lock helpers for routes that mutate model lifecycle."""

from __future__ import annotations

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


__all__ = ["locked"]
