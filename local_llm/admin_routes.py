"""Thin runtime administration routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Response
from fastapi.responses import JSONResponse

from local_llm.config import ModelRecord
from local_llm.locking import locked
from local_llm.model_supply import supply_for
from local_llm.registry import DuplicateAliasError, ModelState, UnknownAliasError


def _ensure_alias_available(registry: Any, alias: str) -> None:
    try:
        registry.get(alias)
    except UnknownAliasError:
        return
    raise DuplicateAliasError(alias)


def create_admin_router(services: Any) -> APIRouter:
    router = APIRouter(prefix="/admin")

    @router.post("/models", status_code=201)
    def add_model(request: ModelRecord) -> JSONResponse:
        _ensure_alias_available(services.registry, request.alias)
        services.preflight.check_download(services.root)
        path = supply_for(services).resolve(request, services.root)
        services.registry.add(request)
        services.registry.mark_downloaded(request.alias, path)
        return JSONResponse(status_code=201, content=request.model_dump(mode="json"))

    @router.post("/models/{alias}/unload")
    def unload(alias: str) -> dict[str, Any]:
        with locked(services.lock, services.settings.server.lock_timeout_seconds):
            entry = services.registry.get(alias)
            if entry.state != ModelState.LOADED or not services.runtime.is_loaded():
                return {"alias": alias, "unloaded": False}

            services.runtime.unload()
            services.registry.set_state(alias, ModelState.UNLOADED)
            return {"alias": alias, "unloaded": True}

    @router.delete("/models/{alias}", status_code=204)
    def remove(alias: str) -> Response:
        with locked(services.lock, services.settings.server.lock_timeout_seconds):
            services.registry.get(alias)
            purge = getattr(services.cache, "remove_alias", None)
            if callable(purge):
                purge(alias)
            services.registry.remove(alias)
        return Response(status_code=204)

    return router


__all__ = ["create_admin_router"]
