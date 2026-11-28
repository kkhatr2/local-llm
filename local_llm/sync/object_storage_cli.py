"""CLI entrypoint for pre-serving object-storage model sync.

Credentials are read only from the process environment (populated from a
Kubernetes Secret or workload identity). They are never read from YAML, passed
as arguments, or logged.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Callable

from local_llm.config import ConfigLoader
from local_llm.sync.object_storage import (
    ObjectCredentials,
    ObjectStorageError,
    ObjectStorageSync,
)

LOGGER = logging.getLogger(__name__)


def _default_client_factory() -> Any:
    import httpx

    return httpx.Client(timeout=30.0)


def _environment_credentials(env: Any) -> ObjectCredentials:
    return ObjectCredentials(
        access_key_id=str(env.get("LOCAL_LLM_OBJECT_ACCESS_KEY_ID", "")),
        secret_access_key=str(env.get("LOCAL_LLM_OBJECT_SECRET_ACCESS_KEY", "")),
        session_token=str(env.get("LOCAL_LLM_OBJECT_SESSION_TOKEN", "")),
    )


def main(
    argv: list[str] | None = None,
    *,
    config_path: str | Path | None = None,
    env: Any = None,
    client_factory: Callable[[], Any] | None = None,
    root: Path | str | None = None,
) -> int:
    """Sync every enabled object-storage model. Return a process exit code."""

    del argv  # No CLI arguments may contain credentials or endpoints.
    environment = env if env is not None else os.environ
    settings = ConfigLoader(env=environment).load(config_path)
    if settings.registry.supply != "object_storage":
        LOGGER.info("object storage sync skipped: supply=%s", settings.registry.supply)
        return 0

    credentials = _environment_credentials(environment)
    if not credentials.access_key_id or not credentials.secret_access_key:
        LOGGER.error("object storage sync rejected: credentials unavailable")
        return 2

    storage = settings.registry.object_storage
    client = (client_factory or _default_client_factory)()
    sync = ObjectStorageSync(
        client,
        credentials,
        storage.bucket,
        storage.prefix,
        storage.region,
        storage.endpoint,
        model_dir=settings.paths.model_dir,
    )
    workspace = Path(root or Path.cwd()).resolve()
    try:
        for record in settings.models:
            if not record.enabled:
                continue
            sync.sync(record, workspace)
    except ObjectStorageError:
        return 1
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()
    return 0


if __name__ == "__main__":  # pragma: no cover - executed only in a pod
    raise SystemExit(main())


__all__ = ["main"]
