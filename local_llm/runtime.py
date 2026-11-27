"""Runtime adapter for llama-cpp-python chat completion."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

logger = logging.getLogger(__name__)

LOAD_OPTION_KEYS = ("n_ctx", "n_batch", "flash_attn", "n_threads", "n_gpu_layers", "verbose")
SAMPLING_OPTION_KEYS = (
    "temperature",
    "top_p",
    "top_k",
    "min_p",
    "repeat_penalty",
    "max_tokens",
    "frequency_penalty",
    "presence_penalty",
    "seed",
    "n",
    "stop",
)


def _as_mapping(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        result = model_dump()
        if isinstance(result, Mapping):
            return dict(result)
        return {}
    try:
        return dict(value)
    except (TypeError, ValueError):
        return {}


def merge_load_options(
    llama_defaults: Any = None,
    model_options: Any = None,
    request_options: Any = None,
) -> dict[str, Any]:
    load_keys = set(LOAD_OPTION_KEYS)
    result: dict[str, Any] = {}
    for source in (llama_defaults, model_options, request_options):
        for key, value in _as_mapping(source).items():
            if key in load_keys:
                result[key] = value
    return result


def merge_sampling_options(
    generation_defaults: Any = None,
    model_options: Any = None,
    request_fields: Any = None,
    request_options: Any = None,
) -> dict[str, Any]:
    sampling_keys = set(SAMPLING_OPTION_KEYS)
    result: dict[str, Any] = {}
    for key, value in _as_mapping(generation_defaults).items():
        if key in sampling_keys and value is not None:
            result[key] = value
    for source in (model_options, request_fields, request_options):
        for key, value in _as_mapping(source).items():
            if key in sampling_keys and value is not None:
                result[key] = value
    return result


@dataclass
class ChatRequest:
    messages: list[dict[str, Any]]
    model: str | None = None
    stream: bool = False
    temperature: Any = None
    top_p: Any = None
    top_k: Any = None
    min_p: Any = None
    repeat_penalty: Any = None
    max_tokens: Any = None
    frequency_penalty: Any = None
    presence_penalty: Any = None
    seed: Any = None
    n: Any = None
    stop: Any = None
    options: Mapping[str, Any] | None = None


class LlamaRuntimeAdapter:
    def __init__(
        self,
        llama_factory: Any,
        *,
        llama_defaults: Any = None,
        generation_defaults: Any = None,
        idle_unload_seconds: float = 180.0,
        clock: Callable[[], float] | None = None,
        lock: Any = None,
    ) -> None:
        self._factory = llama_factory
        self._llama_defaults = _as_mapping(llama_defaults)
        self._generation_defaults = _as_mapping(generation_defaults)
        self._instance: Any = None
        self._model_sampling: dict[str, Any] = {}
        self._loaded_alias: str | None = None
        self._idle_unload_seconds = float(idle_unload_seconds)
        if not 0 < self._idle_unload_seconds <= 86400:
            raise ValueError("idle_unload_seconds must be > 0 and <= 86400")
        self._clock = clock or time.monotonic
        self._lock = lock
        self._idle_state_lock = threading.Lock()
        self._idle_event: threading.Event | None = None
        self._idle_deadline: float | None = None
        self._idle_callback: Callable[[], None] | None = None

    def _request_mapping(self, request: Any) -> dict[str, Any]:
        if isinstance(request, Mapping):
            return dict(request)
        if hasattr(request, "__dict__"):
            try:
                return dict(vars(request))
            except TypeError:
                return {}
        return {}

    def load(
        self, model_path: Any, options: Any = None, *, alias: str | None = None
    ) -> None:
        self.cancel_idle_unload()
        normalized = _as_mapping(options)
        load_options = merge_load_options(self._llama_defaults, normalized, {})
        try:
            instance = self._factory(model_path=model_path, **load_options)
        except Exception:
            self._instance = None
            self._loaded_alias = None
            self._model_sampling = {}
            raise
        self._instance = instance
        self._loaded_alias = alias
        self._model_sampling = merge_sampling_options(None, normalized, None, None)

    @property
    def loaded_alias(self) -> str | None:
        return self._loaded_alias

    @property
    def idle_deadline(self) -> float | None:
        return self._idle_deadline

    def is_loaded(self) -> bool:
        return self._instance is not None

    def cancel_idle_unload(self) -> None:
        """Cancel any scheduled idle unload and return a fresh timer state."""

        with self._idle_state_lock:
            event = self._idle_event
            self._idle_event = None
            self._idle_deadline = None
            self._idle_callback = None
        if event is not None:
            event.set()

    def _start_idle_timer(self, callback: Callable[[], None] | None) -> float:
        """Replace the active timer. Caller must hold ``_idle_state_lock``."""

        previous = self._idle_event
        deadline = self._clock() + self._idle_unload_seconds
        event = threading.Event()
        self._idle_event = event
        self._idle_deadline = deadline
        self._idle_callback = callback
        threading.Thread(
            target=self._idle_worker,
            args=(event, deadline),
            name="local-llm-idle-unload",
            daemon=True,
        ).start()
        if previous is not None:
            previous.set()
        return deadline

    def schedule_idle_unload(
        self, *, on_unload: Callable[[], None] | None = None
    ) -> float | None:
        """Keep the current GGUF loaded until one idle window passes."""

        if not self.is_loaded():
            return None
        with self._idle_state_lock:
            return self._start_idle_timer(on_unload)

    def refresh_idle_unload(self) -> float | None:
        """Reset the idle window when another prompt arrives."""

        if not self.is_loaded():
            return None
        with self._idle_state_lock:
            callback = self._idle_callback
            if callback is None:
                return None
            return self._start_idle_timer(callback)

    def _idle_worker(self, event: threading.Event, deadline: float) -> None:
        remaining = max(0.0, deadline - self._clock())
        if event.wait(remaining):
            return
        self._expire_idle(event, deadline)

    def _expire_idle(self, event: threading.Event, deadline: float) -> None:
        """Unload only if this timer is still current and its deadline passed."""

        with self._idle_state_lock:
            if self._idle_event is not event or self._idle_deadline != deadline:
                return

        lock = self._lock
        acquired = lock is not None and bool(lock.acquire())
        try:
            with self._idle_state_lock:
                if self._idle_event is not event or self._idle_deadline != deadline:
                    return
                callback = self._idle_callback
            if not self.is_loaded() or self._clock() < deadline:
                return
            self.unload()
            # Keep registry state consistent with the runtime while the
            # lifecycle lock is held. Callbacks must not re-enter this lock.
            if callback is not None:
                try:
                    callback()
                except Exception:
                    logger.exception("idle unload callback failed")
        finally:
            if acquired and lock is not None:
                lock.release()

    def unload(self) -> None:
        self.cancel_idle_unload()
        old = self._instance
        self._instance = None
        self._loaded_alias = None
        self._model_sampling = {}
        if old is not None and hasattr(old, "close"):
            try:
                old.close()
            except Exception:
                pass

    def _sampling_for(self, request: Any) -> dict[str, Any]:
        mapping = self._request_mapping(request)
        request_options = _as_mapping(mapping.get("options"))
        return merge_sampling_options(
            self._generation_defaults,
            self._model_sampling,
            mapping,
            request_options,
        )

    def chat(self, request: Any) -> Any:
        if not self.is_loaded():
            raise RuntimeError("model is not loaded")
        mapping = self._request_mapping(request)
        messages = mapping.get("messages")
        if not messages:
            raise ValueError("messages is required")
        sampling = self._sampling_for(request)
        return self._instance.create_chat_completion(
            messages=messages, stream=False, **sampling
        )


__all__ = [
    "ChatRequest",
    "LOAD_OPTION_KEYS",
    "LlamaRuntimeAdapter",
    "SAMPLING_OPTION_KEYS",
    "merge_load_options",
    "merge_sampling_options",
]