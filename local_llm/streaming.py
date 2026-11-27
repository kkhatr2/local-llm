"""OpenAI-compatible live SSE framing and completed-response assembly."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from local_llm.cache import canonical_json

SSE_DONE = "data: [DONE]\n\n"


def sse_data(payload: Mapping[str, Any]) -> str:
    return "data: " + canonical_json(payload) + "\n\n"


class CompletionAccumulator:
    """Assemble one completed chat response from OpenAI-shaped stream chunks."""

    def __init__(self) -> None:
        self._id = "chatcmpl-local"
        self._created = 0
        self._model = ""
        self._choices: dict[int, dict[str, Any]] = {}
        self._usage: dict[str, Any] | None = None
        self._seen_chunk = False

    def add(self, chunk: Mapping[str, Any]) -> None:
        if not isinstance(chunk, Mapping):
            raise ValueError("stream chunk must be a mapping")

        self._seen_chunk = True
        self._id = chunk.get("id", self._id)
        self._created = chunk.get("created", self._created)
        self._model = chunk.get("model", self._model)

        choices = chunk.get("choices") or []
        if not isinstance(choices, Sequence) or isinstance(choices, (str, bytes)):
            raise ValueError("stream chunk choices must be a sequence")

        for position, choice in enumerate(choices):
            if not isinstance(choice, Mapping):
                continue
            index = choice.get("index", position)
            if not isinstance(index, int):
                index = position
            state = self._choices.setdefault(
                index,
                {"role": "assistant", "parts": [], "finish_reason": None},
            )
            delta = choice.get("delta") or {}
            if isinstance(delta, Mapping):
                role = delta.get("role")
                if isinstance(role, str):
                    state["role"] = role
                content = delta.get("content")
                if isinstance(content, str):
                    state["parts"].append(content)
            if choice.get("finish_reason") is not None:
                state["finish_reason"] = choice["finish_reason"]

        usage = chunk.get("usage")
        if isinstance(usage, Mapping):
            self._usage = dict(usage)

    def response(self) -> dict[str, Any]:
        if not self._seen_chunk:
            raise RuntimeError("model returned an empty stream")

        response: dict[str, Any] = {
            "id": self._id,
            "object": "chat.completion",
            "created": self._created,
            "model": self._model,
            "choices": [
                {
                    "index": index,
                    "message": {
                        "role": state["role"],
                        "content": "".join(state["parts"]),
                    },
                    "finish_reason": state["finish_reason"],
                }
                for index, state in sorted(self._choices.items())
            ],
        }
        if self._usage is not None:
            response["usage"] = dict(self._usage)
        return response


__all__ = ["CompletionAccumulator", "SSE_DONE", "sse_data"]
