"""Fail-closed GPU/CPU placement checks before a model is loaded."""

from __future__ import annotations

from dataclasses import dataclass

from local_llm.compute.backend_selection import BackendProbe
from local_llm.config import ResourceSettings
from local_llm.preflight import ResourceError

GIB = 1024**3


class PlacementError(ResourceError):
    """Raised when the requested backend cannot safely host the model."""


@dataclass(frozen=True)
class Placement:
    backend: str
    n_gpu_layers: int
    vram_ok: bool


class DevicePlacement:
    """Validate backend availability and VRAM before llama.cpp load."""

    def __init__(self, resources: ResourceSettings, probes: BackendProbe) -> None:
        self.resources = resources
        self.probes = probes

    def validate(
        self,
        backend: str,
        model_size_bytes: int,
        *,
        n_gpu_layers: int = 0,
    ) -> Placement:
        if backend == "cpu":
            return Placement(backend="cpu", n_gpu_layers=0, vram_ok=True)

        if backend != "cuda":
            raise PlacementError(f"unsupported compute backend: {backend}")

        if not self.probes.cuda_available():
            raise PlacementError("requested backend cuda is unavailable")

        limit_gb = self.resources.cuda_vram_limit_gb
        if limit_gb <= 0:
            raise PlacementError(
                "resources.cuda_vram_limit_gb must be > 0 for backend cuda"
            )

        estimated_gb = model_size_bytes / GIB
        if estimated_gb > limit_gb:
            raise PlacementError(
                f"backend cuda estimated_vram_gb {estimated_gb:.2f} exceeds "
                f"configured limit {limit_gb:.2f}"
            )

        probe_vram = self.probes.cuda_vram_bytes()
        if probe_vram is not None and model_size_bytes > probe_vram:
            raise PlacementError(
                f"backend cuda model size exceeds available vram bytes {probe_vram}"
            )

        return Placement(
            backend="cuda",
            n_gpu_layers=max(0, int(n_gpu_layers)),
            vram_ok=True,
        )


__all__ = ["DevicePlacement", "Placement", "PlacementError", "GIB"]
