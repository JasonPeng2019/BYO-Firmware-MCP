#!/usr/bin/env python3
"""Thin wrapper over the shared Stage 1 smoke verifier module."""

from __future__ import annotations

from pyocd_debug_mcp import reference_smoke as _impl
from pyocd_debug_mcp.reference_smoke import main


def __getattr__(name: str) -> object:
    return getattr(_impl, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(dir(_impl)))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
