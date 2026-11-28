"""Shared containment checks for model files on local volumes."""

from __future__ import annotations

import re
from pathlib import Path

_ALIAS = re.compile(r"^[a-z0-9_-]+$")


def safe_model_path(
    root: Path | str, model_dir: str, alias: str, filename: str
) -> Path:
    """Return a model path that cannot escape the configured model root."""

    if not _ALIAS.fullmatch(alias):
        raise ValueError("invalid model alias")
    if not filename or Path(filename).name != filename or "/" in filename or "\\" in filename:
        raise ValueError("invalid model filename")

    base = (Path(root) / model_dir).resolve()
    candidate = base / alias / filename
    resolved = candidate.resolve()

    if not resolved.is_relative_to(base):
        raise ValueError("model path escapes the model directory")
    for part in (base, base / alias, candidate):
        if part.is_symlink():
            raise ValueError("model path may not traverse a symlink")
    return candidate


__all__ = ["safe_model_path"]
