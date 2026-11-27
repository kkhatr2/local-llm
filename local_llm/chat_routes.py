"""Thin OpenAI-compatible chat and model-listing routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from local_llm.cache import (
    CACHE_EXCLUDED_REQUEST_FIELDS,
    make_cache_key,
    weight_fingerprint,
)
from local_llm.locking import locked
from local_llm.registry import ModelState, UnknownAliasError
from local_llm.runtime import (
    SAMPLING_OPTION_KEYS,
    merge_load_options,
    merge_sampling_options,
)


class ChatCompletionRequest(BaseModel):
    messages: list[dict[str, Any]]
    model: str | None = None
    stream: bool = False
    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    min_p: float | None = None
    repeat_penalty: float | None = None
    max_tokens: int | None = None
    frequency_penalty: float | None = None
    presence_penalty: float | None = None
    seed: int | None = None
    n: int | None = None
    stop: Any = None
    options: dict[str, Any] = Field(default_factory=dict)


def _resolve_path(services: Any, alias: str) -> Any:
    """Download a missing GGUF under the process lock, re-checking first."""

    with locked(services.lock, services.settings.server.lock_timeout_seconds):
        entry = services.registry.get(alias)
        path = entry.path
        if path is not None and services.downloader.exists(
            entry.record, services.root
        ):
            return path
        services.preflight.check_download(services.root)
        path = services.downloader.download(entry.record, services.root)
        services.registry.mark_downloaded(alias, path)
        return path


def _mark_unloaded(services: Any, alias: str) -> None:
    try:
        services.registry.set_state(alias, ModelState.UNLOADED)
    except UnknownAliasError:
        pass


def _cache_response(services: Any, request: Any, cached: Any) -> Any:
    """Return a cached response and treat the prompt as model activity."""

    services.runtime.refresh_idle_unload()
    if request.stream:
        return StreamingResponse(
            services.cache.replay_sse(cached), media_type="text/event-stream"
        )
    return JSONResponse(cached)


def create_chat_router(services: Any) -> APIRouter:
    router = APIRouter()

    @router.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @router.get("/v1/models")
    def models() -> dict[str, Any]:
        return {
            "object": "list",
            "data": [
                {
                    "id": record.alias,
                    "object": "model",
                    "created": 0,
                    "owned_by": "local",
                }
                for record in services.registry.list()
            ],
        }

    @router.post("/v1/models/{alias}/chat/completions")
    def chat(alias: str, request: ChatCompletionRequest) -> Any:
        entry = services.registry.get(alias)
        body = request.model_dump()
        options = body.get("options") or {}

        params_body = dict(body)
        messages = params_body.pop("messages")
        params_body.pop("options", None)
        params = {
            key: value
            for key, value in params_body.items()
            if key not in CACHE_EXCLUDED_REQUEST_FIELDS
        }

        path = entry.path
        if path is None or not services.downloader.exists(entry.record, services.root):
            path = _resolve_path(services, alias)
            entry = services.registry.get(alias)

        fingerprint = weight_fingerprint(path)
        request_fields = {
            key: value
            for key, value in body.items()
            if key in SAMPLING_OPTION_KEYS
        }
        runtime_options = {
            "load": merge_load_options(
                services.settings.llama_defaults, entry.record.options, options
            ),
            "sampling": merge_sampling_options(
                services.settings.generation_defaults,
                entry.record.options,
                request_fields,
                options,
            ),
        }
        key = make_cache_key(
            alias=alias,
            revision=entry.record.revision,
            fingerprint=fingerprint,
            messages=messages,
            params=params,
            runtime=runtime_options,
        )

        cached = services.cache.get(key)
        if cached is not None:
            return _cache_response(services, request, cached)

        with locked(services.lock, services.settings.server.lock_timeout_seconds):
            cached = services.cache.get(key)
            if cached is not None:
                return _cache_response(services, request, cached)

            services.runtime.cancel_idle_unload()
            loaded_alias = getattr(services.runtime, "loaded_alias", None)
            if services.runtime.is_loaded() and loaded_alias != alias:
                if loaded_alias is not None:
                    _mark_unloaded(services, loaded_alias)
                services.runtime.unload()
            reuse_loaded_model = (
                services.runtime.is_loaded() and loaded_alias == alias
            )
            if not reuse_loaded_model:
                services.preflight.check_load(path)
                services.runtime.load(
                    path, {**entry.record.options, **options}, alias=alias
                )
                services.registry.set_state(alias, ModelState.LOADED)

            try:
                generation_request = dict(body)
                generation_request["stream"] = False
                response = services.runtime.chat(generation_request)
                services.cache.put(
                    key,
                    alias=alias,
                    revision=entry.record.revision,
                    response=response,
                )
            except BaseException:
                services.runtime.unload()
                _mark_unloaded(services, alias)
                raise

            services.runtime.schedule_idle_unload(
                on_unload=lambda: _mark_unloaded(services, alias)
            )

            if request.stream:
                return StreamingResponse(
                    services.cache.replay_sse(response), media_type="text/event-stream"
                )
            return JSONResponse(response)

    return router
