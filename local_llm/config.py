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
    lock_timeout_seconds: int = Field(default=600, gt=0)
    idle_unload_seconds: int = Field(default=180, gt=0, le=86400)
    # Server-side request body ceiling; oversized bodies are rejected with 422
    # before any lock is taken or model path is resolved.
    max_request_body_bytes: int = Field(default=1048576, gt=0)


def _validate_relative_path(
    value: str, field_name: str, *, basename_only: bool
) -> str:
    """Reject absolute, parent-traversing, or non-normalized configured paths."""

    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty relative path")
    if "\x00" in value:
        raise ValueError(f"{field_name} must not contain null bytes")
    if "\\" in value:
        raise ValueError(f"{field_name} must use forward slashes only")
    if value.startswith("/") or re.match(r"^[A-Za-z]:", value):
        raise ValueError(f"{field_name} must be a relative path")
    parts = value.split("/")
    if any(part == "" for part in parts):
        raise ValueError(f"{field_name} must be a normalized relative path")
    if any(part == ".." for part in parts):
        raise ValueError(f"{field_name} must not contain '..'")
    if basename_only and len(parts) != 1:
        raise ValueError(f"{field_name} must be a single directory name")
    return value


class PathSettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    model_dir: str = "models"
    cache_dir: str = ".cache/responses"

    @field_validator("model_dir")
    @classmethod
    def _model_dir_valid(cls, value: str) -> str:
        return _validate_relative_path(
            value, "paths.model_dir", basename_only=True
        )

    @field_validator("cache_dir")
    @classmethod
    def _cache_dir_valid(cls, value: str) -> str:
        # The committed MVP value is `.cache/responses`, so nested relative
        # directories remain valid; traversal and absolute paths do not.
        return _validate_relative_path(
            value, "paths.cache_dir", basename_only=False
        )


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

    command: str = "hf"
    timeout_seconds: int = Field(default=1800, gt=0)
    public_only: bool = True
    max_parallel_downloads: int = Field(default=4, gt=0, le=16)


class ResourceSettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    min_free_disk_gb: float = Field(default=10.0, gt=0)
    reserve_disk_gb: float = Field(default=2.0, ge=0)
    max_estimated_model_memory_gb: float = Field(default=6.0, gt=0)
    reserve_system_memory_gb: float = Field(default=4.0, ge=0)
    fail_closed: bool = True
    # Conservative default: 0 means "not configured". CPU placement is
    # unaffected, but selecting CUDA with 0 fails closed (503) instead of
    # silently falling back to CPU.
    cuda_vram_limit_gb: float = Field(default=0.0, ge=0)


class IngressSettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    enabled: bool = False
    gateway_policy: str = "api_key"
    api_key_secret_ref: str = "local-llm-api-key"
    publish: str = "loopback"

    @model_validator(mode="after")
    def _validate_ingress(self) -> "IngressSettings":
        if self.gateway_policy != "api_key":
            raise ValueError(
                "deployment.ingress.gateway_policy must be 'api_key'"
            )
        if self.publish not in ("loopback", "public"):
            raise ValueError(
                "deployment.ingress.publish must be 'loopback' or 'public'"
            )
        if self.enabled and not self.api_key_secret_ref:
            raise ValueError(
                "deployment.ingress.api_key_secret_ref must be non-empty when "
                "deployment.ingress.enabled is true"
            )
        return self


class DeploymentSettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    primary_target: str = "compose"
    local_mode: bool = True
    terraform_reference: str = "aws"
    ingress: IngressSettings = Field(default_factory=IngressSettings)

    @model_validator(mode="after")
    def _validate_deployment(self) -> "DeploymentSettings":
        if self.primary_target not in ("kubernetes", "compose"):
            raise ValueError(
                "deployment.primary_target must be 'kubernetes' or 'compose'"
            )
        if not self.terraform_reference:
            raise ValueError("deployment.terraform_reference must be non-empty")
        return self


class BackendDefaults(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    cpu: dict[str, Any] = Field(
        default_factory=lambda: {"n_gpu_layers": 0}
    )
    cuda: dict[str, Any] = Field(
        default_factory=lambda: {"n_gpu_layers": 33}
    )
    metal: dict[str, Any] = Field(
        default_factory=lambda: {"n_gpu_layers": 1}
    )


class ComputeSettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    default_backend: str = "cpu"
    active_backends: tuple[str, ...] = ("cpu", "cuda")
    documented_backends: tuple[str, ...] = ("metal",)
    device_placement: str = "auto"
    fallback: str = "fail_closed"
    backend_defaults: BackendDefaults = Field(default_factory=BackendDefaults)

    @model_validator(mode="after")
    def _validate_compute(self) -> "ComputeSettings":
        active = tuple(self.active_backends)
        if not active or not set(active).issubset({"cpu", "cuda"}):
            raise ValueError(
                "compute.active_backends must be a non-empty subset of "
                "['cpu', 'cuda']"
            )
        if "metal" in active:
            raise ValueError(
                "compute.active_backends must not include 'metal'"
            )
        if not set(self.documented_backends).issubset(
            {"cpu", "cuda", "metal"}
        ):
            raise ValueError(
                "compute.documented_backends must contain only "
                "['cpu', 'cuda', 'metal']"
            )
        if self.fallback != "fail_closed":
            raise ValueError("compute.fallback must be 'fail_closed'")
        if self.device_placement not in ("auto", "manual"):
            raise ValueError(
                "compute.device_placement must be 'auto' or 'manual'"
            )
        if self.default_backend not in active:
            raise ValueError(
                "compute.default_backend must be listed in "
                "compute.active_backends"
            )
        allowed = set(active) | set(self.documented_backends)
        provided = set(self.backend_defaults.model_fields_set)
        if not provided.issubset(allowed):
            raise ValueError(
                "compute.backend_defaults keys must be a subset of "
                "compute.active_backends plus compute.documented_backends"
            )
        return self


class ObjectStorageSettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider: str = "s3"
    bucket: str = "local-llm-models"
    prefix: str = "gguf/"
    region: str = "us-east-1"
    secret_ref: str = "model-supply-credentials"
    # Optional override for S3-compatible endpoints. Empty means the provider
    # default; the sync CLI never logs this value or credentials.
    endpoint: str = ""


class SyncSettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    enabled: bool = False
    immutable_weights: bool = True
    concurrency: int = Field(default=4, gt=0, le=16)


class RegistrySettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    supply: str = "hf"
    object_storage: ObjectStorageSettings = Field(
        default_factory=ObjectStorageSettings
    )
    sync: SyncSettings = Field(default_factory=SyncSettings)

    @model_validator(mode="after")
    def _validate_registry(self) -> "RegistrySettings":
        if self.supply not in ("hf", "object_storage", "volume"):
            raise ValueError(
                "registry.supply must be one of ['hf', 'object_storage', 'volume']"
            )
        if self.supply == "object_storage" and not (
            self.object_storage.secret_ref
        ):
            raise ValueError(
                "registry.object_storage.secret_ref must be non-empty when "
                "registry.supply is 'object_storage'"
            )
        return self


class SecuritySettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    non_root: bool = True
    read_only_root: bool = True
    secrets_backend: str = "kubernetes_secret"

    @model_validator(mode="after")
    def _validate_security(self) -> "SecuritySettings":
        if self.secrets_backend != "kubernetes_secret":
            raise ValueError(
                "security.secrets_backend must be 'kubernetes_secret'"
            )
        return self


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
    deployment: DeploymentSettings = Field(default_factory=DeploymentSettings)
    compute: ComputeSettings = Field(default_factory=ComputeSettings)
    registry: RegistrySettings = Field(default_factory=RegistrySettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
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
    "BackendDefaults",
    "ComputeSettings",
    "ConfigLoader",
    "DeploymentSettings",
    "GenerationDefaults",
    "IngressSettings",
    "LlamaDefaults",
    "ModelRecord",
    "ObjectStorageSettings",
    "PathSettings",
    "RegistrySettings",
    "ResourceSettings",
    "SecuritySettings",
    "ServerSettings",
    "Settings",
    "SUPPORTED_MODEL_OPTIONS",
    "SyncSettings",
]
