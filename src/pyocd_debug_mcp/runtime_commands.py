"""Child-command selection shared by source and compiled sidecar launches."""

from __future__ import annotations

import os
import sys
from pathlib import Path


class ConfigurationError(ValueError):
    """A sidecar path or protocol argument failed closed validation."""


def is_compiled() -> bool:
    return bool(
        getattr(sys, "frozen", False)
        or "__compiled__" in globals()
        or os.environ.get("BYO_SIDECAR_COMPILED") == "1"
    )


def running_sidecar() -> Path:
    for raw in (sys.argv[0], sys.executable):
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if resolved.is_file():
            return resolved
    raise ConfigurationError("sidecar executable did not resolve to a file")


def sidecar_command(command: str) -> tuple[str, ...]:
    if is_compiled():
        return (str(running_sidecar()), command)
    return (sys.executable, "-m", "pyocd_debug_mcp.sidecar", command)
