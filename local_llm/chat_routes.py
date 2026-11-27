"""Thin OpenAI-compatible chat and model-listing routes."""

from __future__ import annotations

from contextlib import ExitStack
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from local_llm.cache import (
    CACHE_EXCLUDED_REQUEST_FIELDS,
    make_cache_key,
    weight_fingerprint,
)
from local_llm.error_mapper import ErrorMapper
from local_llm.locking import locked
from local_llm.registry import ModelState, UnknownAliasError
from local_llm.runtime import (
    SAMPLING_OPTION_KEYS,
    merge_load_options,
    merge_sampling_options,
)
from local_llm.streaming import CompletionAccumulator, SSE_DONE, sse_data


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


def _has_path(services: Any, entry: Any) -> bool:
    return entry.path is not None and services.downloader.exists(
        entry.record, services.root
    )


def _resolve_path(services: Any, alias: str) -> Any:
    """Download a missing GGUF without holding the lifecycle lock."""

    timeout = services.settings.server.lock_timeout_seconds
    with locked(services.lock, timeout):
        entry = services.registry.get(alias)
        if _has_path(services, entry):
            return entry.path

    with locked(services.download_locks.lock_for(alias), timeout):
        with locked(services.lock, timeout):
            entry = services.registry.get(alias)
            if _has_path(services, entry):
                return entry.path
            services.preflight.check_download(services.root)

        path = services.downloader.download(entry.record, services.root)

        with locked(services.lock, timeout):
            entry = services.registry.get(alias)
            if _has_path(services, entry):
                return entry.path
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


def _ensure_loaded(
    services: Any, alias: str, entry: Any, path: Any, options: dict[str, Any]
) -> None:
    """Prepare the resident model; the caller holds the lifecycle lock."""

    services.runtime.cancel_idle_unload()
    loaded_alias = getattr(services.runtime, "loaded_alias", None)
    if services.runtime.is_loaded() and loaded_alias != alias:
        if loaded_alias is not None:
            _mark_unloaded(services, loaded_alias)
        services.runtime.unload()
    reuse_loaded_model = services.runtime.is_loaded() and loaded_alias == alias
    if not reuse_loaded_model:
        services.preflight.check_load(path)
        services.runtime.load(path, {**entry.record.options, **options}, alias=alias)
        services.registry.set_state(alias, ModelState.LOADED)


def _close_chunks(chunks: Any) -> None:
    close = getattr(chunks, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


def _abandon_stream(services: Any, alias: str, chunks: Any) -> None:
    _close_chunks(chunks)
    services.runtime.unload()
    _mark_unloaded(services, alias)


def _stream_response(
    services: Any,
    *,
    key: str,
    alias: str,
    revision: str,
    chunks: Any,
    lock_stack: ExitStack,
    state: dict[str, bool],
) -> Any:
    state["started"] = True
    succeeded = False
    accumulator = CompletionAccumulator()
    try:
        for chunk in chunks:
            accumulator.add(chunk)
            yield sse_data(chunk)
        response = accumulator.response()
        services.cache.put(key, alias=alias, revision=revision, response=response)
        services.runtime.schedule_idle_unload(
            on_unload=lambda: _mark_unloaded(services, alias)
        )
        succeeded = True
        yield SSE_DONE
    except Exception as exc:
        mapped = ErrorMapper().map(exc)
        yield sse_data(mapped.body)
    finally:
        try:
            if not succeeded:
                _abandon_stream(services, alias, chunks)
        finally:
            lock_stack.close()


class _LifecycleStreamingResponse(StreamingResponse):
    """Release the lifecycle lock even if body iteration never starts."""

    def __init__(
        self,
        *args: Any,
        lock_stack: ExitStack,
        state: dict[str, bool],
        on_never_started: Any,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._lock_stack = lock_stack
        self._state = state
        self._on_never_started = on_never_started

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            try:
                if not self._state["started"]:
                    self._on_never_started()
            finally:
                self._lock_stack.close()


def _stream_chat(
    services: Any,
    *,
    alias: str,
    entry: Any,
    path: Any,
    key: str,
    request: ChatCompletionRequest,
    options: dict[str, Any],
) -> Any:
    """Start a stream after pre-response errors can still use HTTP envelopes."""

    generation_request = request.model_dump()
    generation_request["stream"] = True
    lock_stack = ExitStack()
    try:
        lock_stack.enter_context(
            locked(services.lock, services.settings.server.lock_timeout_seconds)
        )
        cached = services.cache.get(key)
        if cached is not None:
            lock_stack.close()
            return _cache_response(services, request, cached)
        _ensure_loaded(services, alias, entry, path, options)
        chunks = services.runtime.chat_stream(generation_request)
        state = {"started": False}
        response = _LifecycleStreamingResponse(
            _stream_response(
                services,
                key=key,
                alias=alias,
                revision=entry.record.revision,
                chunks=chunks,
                lock_stack=lock_stack,
                state=state,
            ),
            media_type="text/event-stream",
            lock_stack=lock_stack,
            state=state,
            on_never_started=lambda: _abandon_stream(services, alias, chunks),
        )
    except BaseException:
        lock_stack.close()
        raise
    return response


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

        if request.stream:
            return _stream_chat(
                services,
                alias=alias,
                entry=entry,
                path=path,
                key=key,
                request=request,
                options=options,
            )

        with locked(services.lock, services.settings.server.lock_timeout_seconds):
            cached = services.cache.get(key)
            if cached is not None:
                return _cache_response(services, request, cached)

            _ensure_loaded(services, alias, entry, path, options)

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

            return JSONResponse(response)

    return router
