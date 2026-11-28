"""Framework-agnostic mapping of domain failures to stable HTTP envelopes."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from local_llm.downloader import DownloadTimeoutError, DownloaderError
from local_llm.preflight import ResourceError
from local_llm.registry import DuplicateAliasError, UnknownAliasError


@dataclass(frozen=True)
class MappedError:
    status_code: int
    body: dict[str, Any]


_SAFE_RESOURCE_DETAIL = re.compile(r"^[A-Za-z0-9 ._'<>-]+$")


def _body(code: str, message: str, error_type: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "type": error_type}}


def _resource_message(exception: BaseException) -> str:
    """Expose only a safe domain detail such as the requested backend."""

    detail = str(exception).strip()
    if detail and _SAFE_RESOURCE_DETAIL.fullmatch(detail) and "Traceback" not in detail:
        return f"Required resources are unavailable: {detail}"
    return "Required resources are unavailable."


class ErrorMapper:
    def map(self, exception: BaseException) -> MappedError:
        error_type = type(exception).__name__

        if isinstance(exception, UnknownAliasError):
            return MappedError(
                404, _body("not_found", "Requested model was not found.", error_type)
            )
        if isinstance(exception, DuplicateAliasError):
            return MappedError(
                409, _body("conflict", "Model alias already exists.", error_type)
            )
        if isinstance(exception, ResourceError):
            return MappedError(
                503,
                _body(
                    "resource_unavailable",
                    _resource_message(exception),
                    error_type,
                ),
            )
        if isinstance(exception, DownloadTimeoutError):
            return MappedError(
                504,
                _body("download_timeout", "Model download timed out.", error_type),
            )
        if isinstance(exception, DownloaderError):
            return MappedError(
                502, _body("download_error", "Model download failed.", error_type)
            )
        if isinstance(exception, (ValidationError, ValueError)):
            return MappedError(
                422,
                _body("validation_error", "Request validation failed.", error_type),
            )
        if isinstance(exception, KeyError):
            return MappedError(
                404, _body("not_found", "Requested model was not found.", error_type)
            )
        if isinstance(exception, TimeoutError):
            return MappedError(
                504, _body("runtime_timeout", "Generation timed out.", error_type)
            )
        if isinstance(exception, RuntimeError):
            return MappedError(
                502, _body("runtime_error", "Model runtime failed.", error_type)
            )
        return MappedError(
            500, _body("internal_error", "Internal server error.", error_type)
        )


__all__ = ["ErrorMapper", "MappedError"]
