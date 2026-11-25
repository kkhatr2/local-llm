"""In-memory model registry for the local LLM API."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable

from local_llm.config import ModelRecord


class ModelState(str, Enum):
    REGISTERED = "registered"
    DOWNLOADED = "downloaded"
    UNLOADED = "unloaded"
    LOADED = "loaded"
    FAILED = "failed"


class DuplicateAliasError(ValueError):
    def __init__(self, alias: str) -> None:
        self.alias = alias
        super().__init__(f"duplicate model alias: {alias}")


class UnknownAliasError(KeyError):
    def __init__(self, alias: str) -> None:
        self.alias = alias
        super().__init__(f"unknown model alias: {alias}")


@dataclass(frozen=True)
class RegistryEntry:
    record: ModelRecord
    state: ModelState
    path: Path | None


class ModelRegistry:
    def __init__(self, records: Iterable[ModelRecord] = ()) -> None:
        self._entries: dict[str, RegistryEntry] = {}
        for record in records:
            self.add(record)

    def list(self) -> list[ModelRecord]:
        return [entry.record for entry in self._entries.values()]

    def get(self, alias: str) -> RegistryEntry:
        entry = self._entries.get(alias)
        if entry is None:
            raise UnknownAliasError(alias)
        return entry

    def add(self, record: ModelRecord) -> None:
        if record.alias in self._entries:
            raise DuplicateAliasError(record.alias)
        self._entries[record.alias] = RegistryEntry(
            record=record, state=ModelState.REGISTERED, path=None
        )

    def remove(self, alias: str) -> None:
        if alias not in self._entries:
            raise UnknownAliasError(alias)
        del self._entries[alias]

    def mark_downloaded(self, alias: str, path: Path) -> None:
        entry = self.get(alias)
        self._entries[alias] = RegistryEntry(
            record=entry.record,
            state=ModelState.DOWNLOADED,
            path=path,
        )

    def set_state(self, alias: str, state: ModelState) -> None:
        entry = self.get(alias)
        self._entries[alias] = RegistryEntry(
            record=entry.record, state=state, path=entry.path
        )


__all__ = [
    "DuplicateAliasError",
    "ModelState",
    "ModelRegistry",
    "RegistryEntry",
    "UnknownAliasError",
]