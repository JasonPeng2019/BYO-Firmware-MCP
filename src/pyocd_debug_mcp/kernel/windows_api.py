"""Narrow, runtime-checked access to ctypes APIs that exist only on Windows."""

from __future__ import annotations

import ctypes
import os
from typing import Any


def library(name: str) -> Any:
    """Return one Windows DLL proxy without exposing platform-only ctypes attributes."""

    if os.name != "nt":
        raise OSError(f"Windows library {name!r} is unavailable on {os.name!r}")
    loader = getattr(ctypes, "windll", None)
    if loader is None:
        raise OSError("ctypes does not expose the Windows DLL loader")
    return getattr(loader, name)


def last_error() -> int:
    """Return the calling thread's Windows error code."""

    getter = getattr(ctypes, "get_last_error", None)
    if getter is None:
        raise OSError("ctypes does not expose Windows last-error state")
    return int(getter())
