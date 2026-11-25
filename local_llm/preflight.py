"""Fail-closed resource checks before model download and load."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, Callable

from local_llm.config import ResourceSettings

GIB = 1024**3


class ResourceError(Exception):
    status_code = 503


class ResourcePreflight:
    def __init__(
        self,
        resources: ResourceSettings | None = None,
        *,
        disk_usage: Callable[[Path], Any] | None = None,
        memory_reader: Callable[[], int] | None = None,
        file_size: Callable[[Path], int] | None = None,
    ) -> None:
        self.resources = resources if resources is not None else ResourceSettings()
        self.disk_usage = disk_usage if disk_usage is not None else shutil.disk_usage
        self.memory_reader = (
            memory_reader if memory_reader is not None else self._default_memory_reader
        )
        self.file_size = (
            file_size if file_size is not None else self._default_file_size
        )

    @staticmethod
    def _default_file_size(path: Path) -> int:
        return path.stat().st_size

    @staticmethod
    def _default_memory_reader() -> int:
        try:
            for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
        except (OSError, ValueError, IndexError):
            pass

        try:
            return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_AVPHYS_PAGES")
        except (OSError, ValueError, AttributeError):
            raise ResourceError("unable to determine available memory") from None

    def check_download(self, destination: Path | str) -> None:
        try:
            free = self.disk_usage(Path(destination)).free
        except Exception:
            raise ResourceError("unable to determine available disk space") from None

        required = (
            self.resources.min_free_disk_gb + self.resources.reserve_disk_gb
        ) * GIB
        if free < required:
            raise ResourceError("insufficient disk space")

    def check_load(self, model_path: Path | str) -> None:
        try:
            size = self.file_size(Path(model_path))
        except Exception:
            raise ResourceError("unable to determine model size") from None

        if size / GIB > self.resources.max_estimated_model_memory_gb:
            raise ResourceError("estimated model memory exceeds configured limit")

        try:
            available = self.memory_reader()
        except Exception:
            raise ResourceError("unable to determine available memory") from None

        required = size + self.resources.reserve_system_memory_gb * GIB
        if available < required:
            raise ResourceError("insufficient available memory")


__all__ = ["ResourceError", "ResourcePreflight"]
