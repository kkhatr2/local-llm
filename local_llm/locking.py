"""Shared process-wide lock helpers for routes that mutate model lifecycle."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

from local_llm.preflight import ResourceError


@contextmanager
def locked(lock: Any) -> Iterator[None]:
    if not lock.acquire(timeout=30):
        raise ResourceError("server is busy")
    try:
        yield
    finally:
        lock.release()


__all__ = ["locked"]
