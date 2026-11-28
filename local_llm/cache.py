"""Local cache for completed OpenAI-compatible chat responses."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

CACHE_SCHEMA_VERSION = 1

_SAFE_INSTANCE_ID = re.compile(r"^[A-Za-z0-9._-]+$")

CACHE_EXCLUDED_REQUEST_FIELDS = frozenset(
    {
        "stream",
        "stream_options",
        "request_id",
    }
)
CACHE_EXCLUDED_RUNTIME_FIELDS = frozenset({"verbose"})

Clock = Callable[[], datetime]


@dataclass(frozen=True)
class CacheConfig:
    """Configuration for the on-disk response cache."""

    enabled: bool = True
    dir: Path | str = Path(".cache/responses")
    ttl_days: float = 30.0
    max_size_mb: float = 1024.0
    replay_sse: bool = True
    scope: str = "per_instance"

    def __post_init__(self) -> None:
        object.__setattr__(self, "dir", Path(self.dir))
        if self.ttl_days < 0:
            raise ValueError("cache.ttl_days must be >= 0")
        if self.max_size_mb <= 0:
            raise ValueError("cache.max_size_mb must be > 0")
        if self.scope != "per_instance":
            raise ValueError("cache.scope must be 'per_instance'")


def _sanitize_instance_id(instance_id: str) -> str:
    """Return a single safe path segment for per-instance cache isolation."""

    if (
        not instance_id
        or instance_id in (".", "..")
        or not _SAFE_INSTANCE_ID.fullmatch(instance_id)
    ):
        raise ValueError(
            "cache instance_id must be a safe path segment ([A-Za-z0-9._-]+)"
        )
    return instance_id


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def weight_fingerprint(path: Path | str) -> str:
    """Return a cheap fingerprint that changes when a GGUF file is replaced."""

    stat = Path(path).stat()
    return f"{stat.st_size}:{stat.st_mtime_ns}"


def make_cache_key(
    *,
    alias: str,
    revision: str,
    fingerprint: str,
    messages: Sequence[Mapping[str, Any]],
    params: Mapping[str, Any] | None = None,
    runtime: Mapping[str, Any] | None = None,
) -> str:
    """Build a stable cache key from a validated run request.

    `alias` comes from the registry and `fingerprint` from `weight_fingerprint`;
    both are non-empty by contract and are not re-checked here.
    """

    normalized_params = {
        key: value
        for key, value in (params or {}).items()
        if key not in CACHE_EXCLUDED_REQUEST_FIELDS
    }
    normalized_runtime = {
        key: value
        for key, value in (runtime or {}).items()
        if key not in CACHE_EXCLUDED_RUNTIME_FIELDS
    }

    payload = {
        "schema_version": CACHE_SCHEMA_VERSION,
        "alias": alias,
        "revision": revision,
        "weight_fingerprint": fingerprint,
        "messages": list(messages),
        "params": normalized_params,
        "runtime": normalized_runtime,
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _is_hex_key(key: str) -> bool:
    return len(key) == 64 and all(character in "0123456789abcdef" for character in key)


def _choice_content(choice: Mapping[str, Any]) -> str:
    message = choice.get("message") or choice.get("delta") or {}
    if not isinstance(message, Mapping):
        message = {}
    content = message.get("content")
    if content is None:
        content = choice.get("text", "")

    if isinstance(content, str):
        return content
    if isinstance(content, Sequence):
        parts: list[str] = []
        for part in content:
            if isinstance(part, Mapping):
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return ""


def _sse_event(payload: Mapping[str, Any]) -> str:
    return "data: " + canonical_json(payload) + "\n\n"


def _chat_chunk(
    response: Mapping[str, Any],
    choices: list[dict[str, Any]],
    usage: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    chunk: dict[str, Any] = {
        "id": response.get("id", "cached"),
        "object": "chat.completion.chunk",
        "created": response.get("created", 0),
        "model": response.get("model", ""),
        "choices": choices,
    }
    if usage is not None:
        chunk["usage"] = dict(usage)
    return chunk


def iter_sse_frames(response: Mapping[str, Any]) -> Iterator[str]:
    """Replay a stored complete response as OpenAI-compatible SSE frames."""

    choices = response.get("choices") or []
    if not isinstance(choices, Sequence):
        choices = []

    for index, choice in enumerate(choices):
        if not isinstance(choice, Mapping):
            continue
        choice_index = choice.get("index", index)
        yield _sse_event(
            _chat_chunk(
                response,
                [
                    {
                        "index": choice_index,
                        "delta": {"role": "assistant"},
                        "finish_reason": None,
                    }
                ],
            )
        )

        content = _choice_content(choice)
        if content:
            yield _sse_event(
                _chat_chunk(
                    response,
                    [
                        {
                            "index": choice_index,
                            "delta": {"content": content},
                            "finish_reason": None,
                        }
                    ],
                )
            )

        yield _sse_event(
            _chat_chunk(
                response,
                [
                    {
                        "index": choice_index,
                        "delta": {},
                        "finish_reason": choice.get("finish_reason"),
                    }
                ],
            )
        )

    usage = response.get("usage")
    if isinstance(usage, Mapping):
        yield _sse_event(_chat_chunk(response, [], usage=usage))

    yield "data: [DONE]\n\n"


class ResponseCache:
    """File-backed cache for completed chat completion responses."""

    def __init__(
        self,
        config: CacheConfig | None = None,
        *,
        root: Path | str | None = None,
        clock: Clock | None = None,
        instance_id: str | None = None,
    ) -> None:
        self.config = config or CacheConfig()
        self.root = Path(root or Path.cwd()).resolve()
        self._clock = clock or utc_now
        self.instance_id = (
            _sanitize_instance_id(instance_id) if instance_id is not None else None
        )
        self.dir = self._resolve_dir()

    def _resolve_dir(self) -> Path:
        configured = Path(self.config.dir)
        resolved = (configured if configured.is_absolute() else self.root / configured).resolve()

        if not resolved.is_relative_to(self.root):
            raise ValueError("cache.dir must stay inside the project workspace")

        models_dir = (self.root / "models").resolve()
        if resolved.is_relative_to(models_dir):
            raise ValueError("cache.dir may not point inside models/")

        if self.instance_id is None:
            return resolved

        instance_dir = (resolved / self.instance_id).resolve()
        if instance_dir == resolved or not instance_dir.is_relative_to(resolved):
            raise ValueError("cache instance directory must stay inside cache.dir")
        return instance_dir

    def path_for(self, key: str) -> Path:
        if not _is_hex_key(key):
            raise ValueError("cache key must be a 64-character lowercase SHA-256 digest")
        return self.dir / f"{key}.json"

    def get(self, key: str) -> Mapping[str, Any] | None:
        if not self.config.enabled:
            return None

        path = self.path_for(key)
        entry = self._read_entry(path)
        if entry is None:
            return None

        created_at = _parse_timestamp(entry.get("created_at"))
        if created_at is None:
            self._delete(path)
            return None

        now = self._clock()
        if now - created_at >= timedelta(days=self.config.ttl_days):
            self._delete(path)
            return None

        entry["last_accessed_at"] = now.isoformat()
        try:
            self._atomic_write(path, entry)
        except OSError:
            pass

        return entry.get("response")

    def put(
        self,
        key: str,
        *,
        alias: str,
        revision: str,
        response: Mapping[str, Any],
    ) -> bool:
        if not self.config.enabled:
            return False

        path = self.path_for(key)
        now = self._clock().isoformat()
        entry = {
            "schema_version": CACHE_SCHEMA_VERSION,
            "key": key,
            "created_at": now,
            "last_accessed_at": now,
            "alias": alias,
            "revision": revision,
            "response": dict(response),
        }

        try:
            self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                os.chmod(self.dir, 0o700)
            except OSError:
                pass
            self._atomic_write(path, entry)
            self.evict()
            return True
        except OSError:
            return False

    def replay_sse(self, response: Mapping[str, Any]) -> Iterator[str]:
        if not self.config.replay_sse:
            raise RuntimeError("SSE replay is disabled")
        return iter_sse_frames(response)

    def remove_alias(self, alias: str) -> int:
        """Delete cache entries stored for ``alias``, leaving other aliases intact."""

        if not self.dir.exists():
            return 0

        removed = 0
        for path in self.dir.glob("*.json"):
            try:
                entry = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(entry, dict) and entry.get("alias") == alias:
                if self._delete(path):
                    removed += 1
        return removed

    def evict(self) -> int:
        if not self.dir.exists():
            return 0

        now = self._clock()
        limit_bytes = int(self.config.max_size_mb * 1024 * 1024)
        candidates: list[tuple[datetime, int, Path]] = []
        total_bytes = 0
        removed = 0

        for path in self.dir.glob("*.json"):
            entry = self._read_entry(path)
            if entry is None:
                continue

            created_at = _parse_timestamp(entry.get("created_at"))
            if created_at is None or now - created_at >= timedelta(days=self.config.ttl_days):
                self._delete(path)
                removed += 1
                continue

            try:
                size = path.stat().st_size
            except OSError:
                continue

            accessed_at = _parse_timestamp(entry.get("last_accessed_at")) or created_at
            candidates.append((accessed_at, size, path))
            total_bytes += size

        if total_bytes <= limit_bytes:
            return removed

        candidates.sort(key=lambda item: item[0])
        for _, size, path in candidates:
            if total_bytes <= limit_bytes:
                break
            if self._delete(path):
                total_bytes -= size
                removed += 1

        return removed

    def _read_entry(self, path: Path) -> dict[str, Any] | None:
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            self._delete(path)
            return None

        if not isinstance(entry, dict):
            self._delete(path)
            return None
        if entry.get("schema_version") != CACHE_SCHEMA_VERSION:
            self._delete(path)
            return None
        if not isinstance(entry.get("response"), Mapping):
            self._delete(path)
            return None
        return entry

    @staticmethod
    def _atomic_write(path: Path, entry: Mapping[str, Any]) -> None:
        descriptor, temp_name = tempfile.mkstemp(
            prefix=f".{path.stem}.",
            suffix=".tmp",
            dir=path.parent,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(entry, handle, sort_keys=True, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
        except Exception:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise

    @staticmethod
    def _delete(path: Path) -> bool:
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False
        except OSError:
            return False


__all__ = [
    "CacheConfig",
    "ResponseCache",
    "iter_sse_frames",
    "make_cache_key",
    "weight_fingerprint",
]
