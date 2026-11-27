"""FastAPI composition helpers shared by production and contract tests."""

from __future__ import annotations

import logging
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from local_llm.cache import ResponseCache
from local_llm.chat_routes import create_chat_router
from local_llm.config import ConfigLoader
from local_llm.downloader import DownloaderError, HfCliDownloader
from local_llm.error_mapper import ErrorMapper, MappedError
from local_llm.locking import AliasDownloadLocks, locked
from local_llm.preflight import ResourceError, ResourcePreflight
from local_llm.registry import DuplicateAliasError, ModelRegistry, UnknownAliasError
from local_llm.runtime import LlamaRuntimeAdapter


@dataclass
class Services:
    """Collaborators injected into the HTTP routers."""

    settings: Any
    root: Path
    registry: Any
    cache: Any
    downloader: Any
    preflight: Any
    runtime: Any
    lock: Any
    download_locks: Any = field(default_factory=AliasDownloadLocks)


def configure_logging() -> None:
    """Emit application status messages in container logs."""

    package_logger = logging.getLogger("local_llm")
    package_logger.setLevel(logging.INFO)
    if not package_logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        package_logger.addHandler(handler)
    package_logger.propagate = False


def _json_error(mapped: MappedError) -> JSONResponse:
    return JSONResponse(status_code=mapped.status_code, content=mapped.body)


def install_error_handlers(app: FastAPI, mapper: ErrorMapper | None = None) -> None:
    """Install stable, sanitized HTTP error envelopes on an app."""

    error_mapper = mapper or ErrorMapper()

    async def domain_handler(request: Any, exc: BaseException) -> JSONResponse:
        return _json_error(error_mapper.map(exc))

    async def validation_handler(
        request: Any, exc: RequestValidationError
    ) -> JSONResponse:
        return _json_error(
            MappedError(
                422,
                {
                    "error": {
                        "code": "validation_error",
                        "message": "Request validation failed.",
                        "type": type(exc).__name__,
                    }
                },
            )
        )

    app.add_exception_handler(RequestValidationError, validation_handler)
    for error_type in (
        UnknownAliasError,
        DuplicateAliasError,
        ResourceError,
        DownloaderError,
        ValidationError,
        KeyError,
        TimeoutError,
        RuntimeError,
        ValueError,
    ):
        app.add_exception_handler(error_type, domain_handler)
    app.add_exception_handler(Exception, domain_handler)


def create_app(
    services: Services,
    *,
    include_admin: bool = False,
    lifespan: Any = None,
) -> FastAPI:
    """Build an app around injected services.

    Production wiring and startup downloads are added by `build_app`; this
    helper keeps route contract tests independent of concrete clients.
    """

    app = FastAPI(title="Local LLM API", lifespan=lifespan)
    app.state.services = services
    app.include_router(create_chat_router(services))
    if include_admin:
        from local_llm.admin_routes import create_admin_router

        app.include_router(create_admin_router(services))
    install_error_handlers(app)
    return app


def _subprocess_runner(command: list[str], timeout: int) -> int:
    try:
        return subprocess.run(command, check=False, timeout=timeout).returncode
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError("model download timed out") from exc


def _hf_repo_info(repo_id: str, revision: str) -> bool:
    try:
        from huggingface_hub import HfApi

        info = HfApi().model_info(repo_id=repo_id, revision=revision)
    except Exception:
        return False
    return not getattr(info, "private", False) and not bool(
        getattr(info, "gated", False)
    )


def _llama_factory(model_path: Any, **options: Any) -> Any:
    from llama_cpp import Llama

    return Llama(model_path=str(model_path), **options)


def _download_one(services: Services, record: Any) -> None:
    path = services.downloader.download(record, services.root)
    with locked(services.lock, services.settings.server.lock_timeout_seconds):
        services.registry.mark_downloaded(record.alias, path)


def _download_configured_models(services: Services) -> None:
    missing = []
    for record in services.settings.models:
        if not record.enabled:
            continue
        if services.downloader.exists(record, services.root):
            path = services.downloader.download(record, services.root)
            with locked(services.lock, services.settings.server.lock_timeout_seconds):
                services.registry.mark_downloaded(record.alias, path)
        else:
            missing.append(record)

    if not missing:
        return

    for record in missing:
        services.preflight.check_download(services.root)

    workers = min(
        services.settings.download.max_parallel_downloads, len(missing)
    )
    if workers == 1:
        for record in missing:
            _download_one(services, record)
        return

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(_download_one, services, record) for record in missing
        ]
    for future in futures:
        future.result()


def _make_lifespan(services: Services) -> Any:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> Any:
        _download_configured_models(services)
        yield

    return lifespan


def build_app(
    config_path: str | Path | None = None,
    *,
    root: Path | str | None = None,
    clock: Callable[[], Any] | None = None,
    runner: Callable[[list[str], int], int] | None = None,
    repo_info: Callable[[str, str], bool] | None = None,
    llama_factory: Callable[..., Any] | None = None,
    preflight: Any = None,
    lock: Any = None,
    startup_download: bool = True,
) -> FastAPI:
    """Construct the production service graph and HTTP application."""

    root_path = Path(root or Path.cwd()).resolve()
    settings = ConfigLoader().load(config_path)
    enabled_records = [record for record in settings.models if record.enabled]
    lifecycle_lock = lock or threading.Lock()

    services = Services(
        settings=settings,
        root=root_path,
        registry=ModelRegistry(enabled_records),
        cache=ResponseCache(settings.cache, root=root_path, clock=clock),
        downloader=HfCliDownloader(
            settings.download,
            path_settings=settings.paths,
            runner=runner or _subprocess_runner,
            repo_info=repo_info or _hf_repo_info,
        ),
        preflight=preflight or ResourcePreflight(settings.resources),
        runtime=LlamaRuntimeAdapter(
            llama_factory or _llama_factory,
            llama_defaults=settings.llama_defaults,
            generation_defaults=settings.generation_defaults,
            idle_unload_seconds=settings.server.idle_unload_seconds,
            lock=lifecycle_lock,
        ),
        lock=lifecycle_lock,
        download_locks=AliasDownloadLocks(),
    )

    app = create_app(
        services,
        include_admin=True,
        lifespan=_make_lifespan(services) if startup_download else None,
    )
    app.state.settings = settings
    return app


__all__ = [
    "Services",
    "build_app",
    "configure_logging",
    "create_app",
    "install_error_handlers",
]
