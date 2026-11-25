"""FastAPI composition helpers shared by production and contract tests."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from local_llm.chat_routes import create_chat_router
from local_llm.downloader import DownloaderError
from local_llm.error_mapper import ErrorMapper, MappedError
from local_llm.preflight import ResourceError
from local_llm.registry import DuplicateAliasError, UnknownAliasError


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


def create_app(services: Services, *, include_admin: bool = False) -> FastAPI:
    """Build an app around injected services.

    Production wiring and startup downloads are added by `build_app`; this
    helper keeps route contract tests independent of concrete clients.
    """

    app = FastAPI(title="Local LLM API")
    app.state.services = services
    app.include_router(create_chat_router(services))
    if include_admin:
        from local_llm.admin_routes import create_admin_router

        app.include_router(create_admin_router(services))
    install_error_handlers(app)
    return app


__all__ = ["Services", "create_app", "install_error_handlers"]
