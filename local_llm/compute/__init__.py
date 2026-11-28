"""Compute backend selection and fail-closed device placement."""

from local_llm.compute.backend_selection import (
    BackendDecision,
    BackendProbe,
    BackendSelector,
)
from local_llm.compute.device_placement import (
    DevicePlacement,
    Placement,
    PlacementError,
)

__all__ = [
    "BackendDecision",
    "BackendProbe",
    "BackendSelector",
    "DevicePlacement",
    "Placement",
    "PlacementError",
]
