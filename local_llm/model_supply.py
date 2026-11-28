"""Dispatch GGUF model supply without touching the lifecycle lock."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from local_llm.config import ModelRecord
from local_llm.downloader import DownloaderError
from local_llm.paths import safe_model_path


class SupplyError(DownloaderError):
    """Raised when a configured supply source cannot provide a model."""


class ModelSupplyPort(Protocol):
    def exists(self, record: ModelRecord, root: Path | str) -> bool: ...

    def download(self, record: ModelRecord, root: Path | str) -> Path: ...


class ObjectStoragePort(Protocol):
    def exists(self, record: ModelRecord, root: Path | str) -> bool: ...

    def sync(self, record: ModelRecord, root: Path | str) -> Path: ...


class ModelSupply:
    """Resolve models from hf, object storage, or a pre-populated volume."""

    def __init__(
        self,
        supply: str,
        downloader: ModelSupplyPort | None = None,
        sync: ObjectStoragePort | None = None,
        *,
        model_dir: str = "models",
    ) -> None:
        if supply not in ("hf", "object_storage", "volume"):
            raise ValueError("registry.supply must be hf, object_storage, or volume")
        self.supply = supply
        self.downloader = downloader
        self.sync = sync
        self.model_dir = model_dir

    def _volume_path(self, record: ModelRecord, root: Path | str) -> Path:
        return safe_model_path(root, self.model_dir, record.alias, record.filename)

    def exists(self, record: ModelRecord, root: Path | str) -> bool:
        if self.supply == "hf":
            if self.downloader is None:
                raise SupplyError("hf downloader is not configured")
            return self.downloader.exists(record, root)
        if self.supply == "object_storage":
            if self.sync is None:
                raise SupplyError("object storage sync is not configured")
            return self.sync.exists(record, root)
        try:
            path = self._volume_path(record, root)
        except ValueError:
            return False
        return path.is_file() and path.stat().st_size > 0

    def resolve(self, record: ModelRecord, root: Path | str) -> Path:
        if self.supply == "hf":
            if self.downloader is None:
                raise SupplyError("hf downloader is not configured")
            return self.downloader.download(record, root)
        if self.supply == "object_storage":
            # Object-storage I/O is an edge operation (initContainer or CLI).
            # The request path may only consume weights already on the volume.
            path = self._volume_path(record, root)
            if not path.is_file() or path.stat().st_size <= 0:
                raise SupplyError(
                    f"object storage weights are not synced: alias={record.alias}"
                )
            return path
        path = self._volume_path(record, root)
        if not path.is_file() or path.stat().st_size <= 0:
            raise SupplyError(f"volume model is missing: alias={record.alias}")
        return path


class _DownloaderSupplyAdapter:
    """Preserve the legacy downloader contract for injected test doubles."""

    def __init__(self, downloader: Any) -> None:
        self.downloader = downloader

    def exists(self, record: ModelRecord, root: Path | str) -> bool:
        return self.downloader.exists(record, root)

    def download(self, record: ModelRecord, root: Path | str) -> Path:
        return self.downloader.download(record, root)

    def resolve(self, record: ModelRecord, root: Path | str) -> Path:
        return self.downloader.download(record, root)


def supply_for(services: Any) -> ModelSupplyPort | ObjectStoragePort:
    """Return the injected supply port, falling back to the hf downloader."""

    configured = getattr(services, "model_supply", None)
    if configured is not None:
        return configured
    downloader = getattr(services, "downloader", None)
    if downloader is None:
        raise SupplyError("no model supply is configured")
    if hasattr(downloader, "resolve"):
        return downloader
    return _DownloaderSupplyAdapter(downloader)


__all__ = [
    "ModelSupply",
    "ModelSupplyPort",
    "ObjectStoragePort",
    "SupplyError",
    "supply_for",
]
