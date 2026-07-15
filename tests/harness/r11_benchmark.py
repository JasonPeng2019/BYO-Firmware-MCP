#!/usr/bin/env python3
"""Thin wrapper over the shared benchmark support module."""

from __future__ import annotations

from pyocd_debug_mcp import benchmark_support as _impl
from pyocd_debug_mcp.benchmark_support import main


def __getattr__(name: str) -> object:
    return getattr(_impl, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(dir(_impl)))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
