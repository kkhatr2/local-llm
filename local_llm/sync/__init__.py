"""Edge-only model synchronization adapters."""

from local_llm.sync.object_storage import (
    ObjectCredentials,
    ObjectStorageError,
    ObjectStorageSync,
)

__all__ = ["ObjectCredentials", "ObjectStorageError", "ObjectStorageSync"]
