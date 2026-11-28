"""Select the configured compute backend without silent fallback."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from local_llm.config import ComputeSettings
from local_llm.preflight import ResourceError


class BackendProbe(Protocol):
    """Injected capability probe; production CUDA probes implement this."""

    def cuda_available(self) -> bool: ...

    def cuda_vram_bytes(self) -> int | None: ...


@dataclass(frozen=True)
class BackendDecision:
    backend: str
    load_options: dict[str, Any]
    reason: str
    available: bool


class BackendSelector:
    """Resolve one backend decision from immutable configuration."""

    def __init__(self, settings: ComputeSettings, probes: BackendProbe) -> None:
        self.settings = settings
        self.probes = probes

    def select(self) -> BackendDecision:
        requested = self.settings.default_backend
        if requested not in self.settings.active_backends:
            raise ResourceError(
                "compute.default_backend is not in compute.active_backends"
            )

        if requested == "cpu":
            available = True
            reason = "cpu is always available"
        elif requested == "cuda":
            available = bool(self.probes.cuda_available())
            reason = "cuda probe available" if available else "cuda probe unavailable"
        else:
            raise ResourceError(f"unsupported compute backend: {requested}")

        if not available and self.settings.fallback != "fail_closed":
            raise ResourceError("compute.fallback must be 'fail_closed'")

        defaults = getattr(self.settings.backend_defaults, requested, {})
        load_options = dict(defaults)
        if requested == "cpu":
            load_options["n_gpu_layers"] = 0
        return BackendDecision(
            backend=requested,
            load_options=load_options,
            reason=reason,
            available=available,
        )


__all__ = ["BackendDecision", "BackendProbe", "BackendSelector"]
