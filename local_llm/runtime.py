"""Runtime adapter for llama-cpp-python chat completion."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator, Mapping

from local_llm.cache import canonical_json

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
    ) -> None:
        self._factory = llama_factory
        self._llama_defaults = _as_mapping(llama_defaults)
        self._generation_defaults = _as_mapping(generation_defaults)
        self._instance: Any = None
        self._model_sampling: dict[str, Any] = {}

    def _request_mapping(self, request: Any) -> dict[str, Any]:
        if isinstance(request, Mapping):
            return dict(request)
        if hasattr(request, "__dict__"):
            try:
                return dict(vars(request))
            except TypeError:
                return {}
        return {}

    def load(self, model_path: Any, options: Any = None) -> None:
        normalized = _as_mapping(options)
        load_options = merge_load_options(self._llama_defaults, normalized, {})
        self._model_sampling = merge_sampling_options(None, normalized, None, None)
        try:
            instance = self._factory(model_path=model_path, **load_options)
        except Exception:
            self._instance = None
            raise
        self._instance = instance

    def is_loaded(self) -> bool:
        return self._instance is not None

    def unload(self) -> None:
        old = self._instance
        self._instance = None
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

    def stream_chat(self, request: Any) -> Iterator[str]:
        if not self.is_loaded():
            raise RuntimeError("model is not loaded")
        mapping = self._request_mapping(request)
        messages = mapping.get("messages")
        if not messages:
            raise ValueError("messages is required")
        sampling = self._sampling_for(request)
        chunks = self._instance.create_chat_completion(
            messages=messages, stream=True, **sampling
        )

        def generator() -> Iterator[str]:
            for chunk in chunks:
                yield "data: " + canonical_json(chunk) + "\n\n"
            yield "data: [DONE]\n\n"

        return generator()


__all__ = [
    "ChatRequest",
    "LOAD_OPTION_KEYS",
    "LlamaRuntimeAdapter",
    "SAMPLING_OPTION_KEYS",
    "merge_load_options",
    "merge_sampling_options",
]