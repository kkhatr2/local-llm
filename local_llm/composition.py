"""FastAPI composition helpers shared by production and contract tests."""

from __future__ import annotations

import logging
import os
import shutil
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
from local_llm.compute import BackendSelector, DevicePlacement
from local_llm.config import ConfigLoader
from local_llm.downloader import DownloaderError, HfCliDownloader
from local_llm.error_mapper import ErrorMapper, MappedError
from local_llm.locking import AliasDownloadLocks, locked
from local_llm.model_supply import ModelSupply
from local_llm.preflight import ResourceError, ResourcePreflight
from local_llm.registry import DuplicateAliasError, ModelRegistry, UnknownAliasError
from local_llm.runtime import LlamaRuntimeAdapter
from local_llm.sync import ObjectCredentials, ObjectStorageSync

LOGGER = logging.getLogger(__name__)


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
    model_supply: Any = None
    backend: Any = None
    device_placement: Any = None
    effective_llama_defaults: Any = None
    instance_id: str | None = None


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

    @app.middleware("http")
    async def reject_oversized_body(request: Any, call_next: Any) -> Any:
        limit = getattr(
            getattr(services.settings, "server", None),
            "max_request_body_bytes",
            None,
        )
        if limit is None or request.method not in {"POST", "PUT", "PATCH"}:
            return await call_next(request)
        declared = request.headers.get("content-length")
        if declared is not None:
            try:
                too_large = int(declared) > int(limit)
            except (TypeError, ValueError):
                too_large = True
            if too_large:
                return JSONResponse(
                    status_code=422,
                    content={
                        "error": {
                            "code": "validation_error",
                            "message": "Request body exceeds the configured limit.",
                            "type": "RequestValidationError",
                        }
                    },
                )
            return await call_next(request)
        body = await request.body()
        if len(body) > int(limit):
            return JSONResponse(
                status_code=422,
                content={
                    "error": {
                        "code": "validation_error",
                        "message": "Request body exceeds the configured limit.",
                        "type": "RequestValidationError",
                    }
                },
            )
        return await call_next(request)

    app.include_router(create_chat_router(services))
    if include_admin:
        from local_llm.admin_routes import create_admin_router

        app.include_router(create_admin_router(services))
    install_error_handlers(app)
    return app


def _subprocess_runner(command: list[str], timeout: int) -> int:
    """Run the CLI without echoing subprocess output into application logs."""

    try:
        completed = subprocess.run(
            command,
            check=False,
            timeout=timeout,
            capture_output=True,
            text=True,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError("model download timed out") from exc
    LOGGER.info("model download subprocess exit_code=%s", completed.returncode)
    return completed.returncode


def _hf_repo_info(repo_id: str, revision: str) -> bool:
    try:
        from huggingface_hub import HfApi

        info = HfApi().model_info(repo_id=repo_id, revision=revision)
    except Exception:
        return False
    return not getattr(info, "private", False) and not bool(
        getattr(info, "gated", False)
    )


class SystemBackendProbe:
    """Conservative production probe; no subprocess output is ever logged."""

    def cuda_available(self) -> bool:
        return shutil.which("nvidia-smi") is not None

    def cuda_vram_bytes(self) -> int | None:
        if not self.cuda_available():
            return None
        try:
            completed = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=memory.total",
                    "--format=csv,noheader,nounits",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
            first = completed.stdout.strip().splitlines()[0]
            return int(float(first.strip())) * 1024 * 1024
        except (IndexError, ValueError, OSError, subprocess.SubprocessError):
            return None


def _httpx_client_factory() -> Any:
    import httpx

    return httpx.Client(timeout=60.0)


def _llama_factory(model_path: Any, **options: Any) -> Any:
    from llama_cpp import Llama

    return Llama(model_path=str(model_path), **options)


def _download_one(services: Services, record: Any) -> None:
    """Re-check preflight under the alias lock, then resolve and register."""

    timeout = services.settings.server.lock_timeout_seconds
    supply = getattr(services, "model_supply", None) or services.downloader
    with locked(services.download_locks.lock_for(record.alias), timeout):
        if supply.exists(record, services.root):
            path = supply.download(record, services.root)
        else:
            services.preflight.check_download(services.root)
            path = supply.resolve(record, services.root)
        with locked(services.lock, timeout):
            services.registry.mark_downloaded(record.alias, path)


def _download_configured_models(services: Services) -> None:
    if getattr(services.settings, "registry", None) is not None:
        if services.settings.registry.supply != "hf":
            return

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
    backend_probes: Any = None,
    object_storage_client: Any = None,
    object_storage_client_factory: Callable[[], Any] | None = None,
    object_storage_credentials: Any = None,
    instance_id: str | None = None,
) -> FastAPI:
    """Construct the production service graph and HTTP application."""

    root_path = Path(root or Path.cwd()).resolve()
    settings = ConfigLoader().load(config_path)
    enabled_records = [record for record in settings.models if record.enabled]
    lifecycle_lock = lock or threading.Lock()
    probes = backend_probes if backend_probes is not None else SystemBackendProbe()

    backend = BackendSelector(settings.compute, probes).select()
    effective_defaults = settings.llama_defaults.model_dump()
    effective_defaults.update(backend.load_options)
    placement = DevicePlacement(settings.resources, probes)

    downloader = HfCliDownloader(
        settings.download,
        path_settings=settings.paths,
        runner=runner or _subprocess_runner,
        repo_info=repo_info or _hf_repo_info,
    )
    object_sync: Any = None
    if settings.registry.supply == "object_storage":
        client = object_storage_client
        if client is None:
            factory = object_storage_client_factory or _httpx_client_factory
            client = factory()
        credentials = object_storage_credentials
        if credentials is None:
            credentials = ObjectCredentials()
        storage = settings.registry.object_storage
        object_sync = ObjectStorageSync(
            client,
            credentials,
            storage.bucket,
            storage.prefix,
            storage.region,
            storage.endpoint,
            model_dir=settings.paths.model_dir,
        )
    model_supply = ModelSupply(
        settings.registry.supply,
        downloader,
        object_sync,
        model_dir=settings.paths.model_dir,
    )
    resolved_instance_id = instance_id or os.environ.get("LOCAL_LLM_INSTANCE_ID")

    services = Services(
        settings=settings,
        root=root_path,
        registry=ModelRegistry(enabled_records),
        cache=ResponseCache(
            settings.cache,
            root=root_path,
            clock=clock,
            instance_id=resolved_instance_id,
        ),
        downloader=downloader,
        preflight=preflight or ResourcePreflight(settings.resources),
        runtime=LlamaRuntimeAdapter(
            llama_factory or _llama_factory,
            llama_defaults=effective_defaults,
            generation_defaults=settings.generation_defaults,
            idle_unload_seconds=settings.server.idle_unload_seconds,
            lock=lifecycle_lock,
        ),
        lock=lifecycle_lock,
        download_locks=AliasDownloadLocks(),
        model_supply=model_supply,
        backend=backend,
        device_placement=placement,
        effective_llama_defaults=effective_defaults,
        instance_id=resolved_instance_id,
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
