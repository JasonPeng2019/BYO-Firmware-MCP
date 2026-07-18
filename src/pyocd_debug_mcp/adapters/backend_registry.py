"""Production target-backend selection without vendor conditionals in Server B."""

from __future__ import annotations

import os
from importlib.metadata import entry_points
from typing import Callable, cast

from pyocd_debug_mcp.adapters.swd_pyocd import PyOCDSWDInterface
from pyocd_debug_mcp.adapters.target_backend import TargetBackend

BackendFactory = Callable[[], TargetBackend]
ENTRY_POINT_GROUP = "pyocd_debug_mcp.target_backends"


def backend_factories() -> dict[str, BackendFactory]:
    factories: dict[str, BackendFactory] = {"pyocd": PyOCDSWDInterface}
    for entry in entry_points(group=ENTRY_POINT_GROUP):
        if entry.name in factories:
            raise RuntimeError(f"duplicate target backend name: {entry.name}")
        loaded = entry.load()
        if not callable(loaded):
            raise RuntimeError(f"target backend entry point is not callable: {entry.name}")
        factories[entry.name] = cast(BackendFactory, loaded)
    return factories


def configured_backend(name: str | None = None) -> TargetBackend:
    name = (name or os.environ.get("BYO_TARGET_BACKEND", "pyocd")).strip().casefold()
    factories = backend_factories()
    factory = factories.get(name)
    if factory is None:
        raise RuntimeError(
            f"unknown BYO_TARGET_BACKEND {name!r}; available={sorted(factories)}"
        )
    backend = factory()
    if not isinstance(backend, TargetBackend):
        raise RuntimeError(f"target backend {name!r} does not implement TargetBackend")
    return backend


def available_backends() -> tuple[TargetBackend, ...]:
    """Instantiate every installed provider for provider-neutral inventory."""

    return tuple(factory() for _, factory in sorted(backend_factories().items()))
