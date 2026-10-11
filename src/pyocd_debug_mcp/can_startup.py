# SPDX-License-Identifier: Apache-2.0
# Adapted from Firmware-CLI commit 0e8a45d for BYO-Firmware-MCP.
"""Immutable, software-only CAN startup contract for BYO-Firmware-MCP.

This BYO-specific module is informed by Firmware-CLI commit ``0e8a45d`` but
deliberately omits its required-mode health and terminal-proof contracts.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
import re
import sys
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Mapping

from pyocd_debug_mcp.can_config import (
    LoadedCanFixtureConfig,
    load_can_fixture_config,
    snapshot_loaded_can_fixture,
)
from pyocd_debug_mcp.can_errors import CanConfigError

_SHA256 = re.compile(r"[0-9a-f]{64}")
_ALLOWED_ENVIRONMENT = frozenset(
    {
        "PYOCD_CAN_MODE",
        "PYOCD_CAN_CONFIG",
        "PYOCD_CAN_CONFIG_SHA256",
        "PYOCD_CAN_RUNTIME_SHA256",
        "PYOCD_CAN_READ_SECONDS",
    }
)
_REJECTED_LEGACY_ENVIRONMENT = frozenset(
    {
        "PYOCD_FIXTURE_EVIDENCE_CONTRACT",
        "PYOCD_FIXTURE_EVIDENCE_CONTRACT_SHA256",
    }
)


class CanMode(str, Enum):
    OFF = "off"
    AVAILABLE = "available"


@dataclass(frozen=True, slots=True)
class CanStartupRuntime:
    schema_version: int
    mode: CanMode
    source_sha256: str
    profile_sha256: str
    effective_read_seconds: float | None

    @property
    def canonical_bytes(self) -> bytes:
        payload = asdict(self)
        payload["mode"] = self.mode.value
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")

    @property
    def canonical_sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes).hexdigest()


@dataclass(frozen=True, slots=True)
class LoadedCanStartup:
    runtime: CanStartupRuntime
    fixture: LoadedCanFixtureConfig | None


def _nonempty(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name, "")
    if value and value != value.strip():
        raise CanConfigError(f"{name} must not contain surrounding whitespace")
    return value


def _validate_environment_names(environment: Mapping[str, str]) -> None:
    unknown = sorted(
        name
        for name, value in environment.items()
        if value
        and (
            (name.startswith("PYOCD_CAN_") and name not in _ALLOWED_ENVIRONMENT)
            or name in _REJECTED_LEGACY_ENVIRONMENT
        )
    )
    if unknown:
        raise CanConfigError(
            "Unsupported or legacy CAN environment variable(s): " + ", ".join(unknown)
        )


def _runtime_digest_matches(environment: Mapping[str, str], runtime: CanStartupRuntime) -> None:
    expected = _nonempty(environment, "PYOCD_CAN_RUNTIME_SHA256")
    if expected and expected != runtime.canonical_sha256:
        raise CanConfigError("CAN runtime SHA-256 mismatch")


def _read_seconds(environment: Mapping[str, str], fixture: LoadedCanFixtureConfig) -> float:
    raw = _nonempty(environment, "PYOCD_CAN_READ_SECONDS")
    if not raw:
        return fixture.config.receive.default_read_seconds
    try:
        value = float(raw)
    except ValueError as exc:
        raise CanConfigError("PYOCD_CAN_READ_SECONDS must be a finite positive number") from exc
    if not math.isfinite(value) or value <= 0:
        raise CanConfigError("PYOCD_CAN_READ_SECONDS must be a finite positive number")
    if value > fixture.config.receive.max_read_seconds:
        raise CanConfigError("PYOCD_CAN_READ_SECONDS exceeds the immutable profile maximum")
    return value


def _require_can_extra(adapter_kind: str) -> None:
    missing = ["python-can"] if importlib.util.find_spec("can") is None else []
    if adapter_kind == "socketcan_vcan":
        if not sys.platform.startswith("linux"):
            raise CanConfigError("socketcan_vcan is available only on Linux")
        if importlib.util.find_spec("pyroute2") is None:
            missing.append("pyroute2")
    if missing:
        raise CanConfigError(
            "Configured CAN requires the optional backend dependencies "
            f"({', '.join(missing)}); install pyocd-debug-mcp[can]"
        )


def load_can_startup(
    environment: Mapping[str, str] | None = None,
    *,
    snapshot_dir: Path | None = None,
) -> LoadedCanStartup:
    """Load, validate, snapshot, and hash CAN authority exactly once."""

    env = os.environ if environment is None else environment
    _validate_environment_names(env)
    path = _nonempty(env, "PYOCD_CAN_CONFIG")
    raw_mode = _nonempty(env, "PYOCD_CAN_MODE") or ("available" if path else "off")
    try:
        mode = CanMode(raw_mode)
    except ValueError as exc:
        raise CanConfigError("PYOCD_CAN_MODE must be one of: off, available") from exc

    if mode is CanMode.OFF:
        forbidden = tuple(
            name
            for name in (
                "PYOCD_CAN_CONFIG",
                "PYOCD_CAN_CONFIG_SHA256",
                "PYOCD_CAN_READ_SECONDS",
            )
            if _nonempty(env, name)
        )
        if forbidden:
            raise CanConfigError(
                "CAN off mode requires empty authority variables: " + ", ".join(forbidden)
            )
        runtime = CanStartupRuntime(
            schema_version=1,
            mode=mode,
            source_sha256="unstaged",
            profile_sha256="unstaged",
            effective_read_seconds=None,
        )
        _runtime_digest_matches(env, runtime)
        return LoadedCanStartup(runtime=runtime, fixture=None)

    if not path:
        raise CanConfigError("PYOCD_CAN_CONFIG is required when CAN mode is available")
    expected_source = _nonempty(env, "PYOCD_CAN_CONFIG_SHA256") or None
    if expected_source is not None and _SHA256.fullmatch(expected_source) is None:
        raise CanConfigError("PYOCD_CAN_CONFIG_SHA256 must be 64 lowercase hex digits")
    fixture = load_can_fixture_config(Path(path), expected_sha256=expected_source)
    if fixture.config.adapter.physical:
        raise CanConfigError(
            "Physical CAN startup is disabled until a separate hardware qualification is accepted"
        )
    _require_can_extra(fixture.config.adapter.kind)
    runtime = CanStartupRuntime(
        schema_version=1,
        mode=mode,
        source_sha256=fixture.source_sha256,
        profile_sha256=fixture.canonical_sha256,
        effective_read_seconds=_read_seconds(env, fixture),
    )
    _runtime_digest_matches(env, runtime)
    if snapshot_dir is not None:
        fixture = snapshot_loaded_can_fixture(fixture, snapshot_dir)
    return LoadedCanStartup(runtime=runtime, fixture=fixture)


__all__ = [
    "CanMode",
    "CanStartupRuntime",
    "LoadedCanStartup",
    "load_can_startup",
]
