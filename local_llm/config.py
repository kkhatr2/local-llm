"""Configuration loading and immutable settings for the local LLM API."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Callable, Mapping

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from local_llm.cache import CacheConfig

SUPPORTED_MODEL_OPTIONS = frozenset(
    {
        "n_ctx",
        "n_batch",
        "flash_attn",
        "n_threads",
        "n_gpu_layers",
        "verbose",
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
    }
)

ALIAS_PATTERN = re.compile(r"^[a-z0-9_-]+$")
REPO_ID_PATTERN = re.compile(r"^[^\s/]+/[^\s/]+$")


class ServerSettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    host: str = "0.0.0.0"
    port: int = Field(default=8000, ge=1, le=65535)


class PathSettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    model_dir: str = "models"
    cache_dir: str = ".cache/responses"


class LlamaDefaults(BaseModel):
    model_config = ConfigDict(frozen=True)

    n_ctx: int = Field(gt=0)
    n_batch: int = Field(gt=0)
    flash_attn: bool
    n_threads: int = Field(default=8, gt=0)
    n_gpu_layers: int = Field(default=0, ge=0)
    verbose: bool = False

    @field_validator("n_threads")
    @classmethod
    def _n_threads_valid(cls, value: int) -> int:
        cpu_count = os.cpu_count()
        if cpu_count is not None and value > cpu_count:
            raise ValueError("llama_defaults.n_threads must be <= os.cpu_count()")
        return value


class GenerationDefaults(BaseModel):
    model_config = ConfigDict(frozen=True)

    temperature: float = 0.1
    top_p: float = 0.2
    top_k: int = 20
    min_p: float = 0.08
    repeat_penalty: float = 1.1
    max_tokens: int = 512
    frequency_penalty: float | None = None
    presence_penalty: float | None = None
    seed: int | None = None
    n: int | None = None
    stop: Any = None


class DownloadSettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    command: str = "huggingface-cli"
    timeout_seconds: int = Field(default=1800, gt=0)
    public_only: bool = True


class ResourceSettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    min_free_disk_gb: float = Field(default=10.0, gt=0)
    reserve_disk_gb: float = Field(default=2.0, ge=0)
    max_estimated_model_memory_gb: float = Field(default=6.0, gt=0)
    reserve_system_memory_gb: float = Field(default=4.0, ge=0)
    fail_closed: bool = True


class ModelRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    alias: str
    enabled: bool = True
    repo_id: str
    filename: str
    revision: str = "main"
    options: dict[str, Any] = Field(default_factory=dict)

    @field_validator("alias")
    @classmethod
    def _alias_valid(cls, value: str) -> str:
        if not value or not ALIAS_PATTERN.fullmatch(value):
            raise ValueError(f"invalid alias: {value!r}")
        return value

    @field_validator("repo_id")
    @classmethod
    def _repo_id_valid(cls, value: str) -> str:
        if not value or not REPO_ID_PATTERN.fullmatch(value):
            raise ValueError(f"invalid repo_id: {value!r}")
        return value

    @field_validator("filename")
    @classmethod
    def _filename_valid(cls, value: str) -> str:
        if not value or "/" in value or "\\" in value:
            raise ValueError(f"invalid filename: {value!r}")
        if Path(value).name != value:
            raise ValueError(f"filename must be a basename: {value!r}")
        if not value.lower().endswith(".gguf"):
            raise ValueError(f"filename must end in .gguf: {value!r}")
        return value

    @field_validator("options")
    @classmethod
    def _options_keys_valid(cls, value: Mapping[str, Any]) -> dict[str, Any]:
        unsupported = set(value) - SUPPORTED_MODEL_OPTIONS
        if unsupported:
            raise ValueError(
                f"unsupported model options: {sorted(unsupported)}"
            )
        return dict(value)


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True)

    server: ServerSettings = Field(default_factory=ServerSettings)
    paths: PathSettings = Field(default_factory=PathSettings)
    llama_defaults: LlamaDefaults
    generation_defaults: GenerationDefaults = Field(
        default_factory=GenerationDefaults
    )
    max_n_ctx: int = Field(default=32768, gt=0)
    cache: CacheConfig = Field(default_factory=CacheConfig)
    download: DownloadSettings = Field(default_factory=DownloadSettings)
    resources: ResourceSettings = Field(default_factory=ResourceSettings)
    models: tuple[ModelRecord, ...] = Field(default_factory=tuple)

    @property
    def cache_config(self) -> CacheConfig:
        return self.cache

    @model_validator(mode="after")
    def _validate_cross_fields(self) -> "Settings":
        if self.llama_defaults.n_ctx > self.max_n_ctx:
            raise ValueError("llama_defaults.n_ctx must be <= max_n_ctx")

        seen: set[str] = set()
        for model in self.models:
            if not model.enabled:
                continue
            if model.alias in seen:
                raise ValueError(f"duplicate model alias: {model.alias}")
            seen.add(model.alias)

        return self


class ConfigLoader:
    def __init__(
        self,
        env: Mapping[str, str] | None = None,
        reader: Callable[[str], str] | None = None,
    ) -> None:
        self.env = env if env is not None else os.environ
        self.reader = reader if reader is not None else self._default_reader

    @staticmethod
    def _default_reader(path: str) -> str:
        return Path(path).read_text(encoding="utf-8")

    def load(self, path: str | Path | None = None) -> Settings:
        if path is None:
            path = self.env.get("LOCAL_LLM_CONFIG")
        if path is None:
            path = "local_llm.yaml"

        text = self.reader(str(path))
        data = yaml.safe_load(text)
        if not isinstance(data, Mapping):
            raise ValueError("config must be a mapping")
        return Settings.model_validate(data)


__all__ = [
    "ConfigLoader",
    "GenerationDefaults",
    "LlamaDefaults",
    "ModelRecord",
    "PathSettings",
    "ResourceSettings",
    "ServerSettings",
    "Settings",
    "SUPPORTED_MODEL_OPTIONS",
]
