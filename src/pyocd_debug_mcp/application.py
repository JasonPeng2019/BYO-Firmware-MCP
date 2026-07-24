"""Explicit, immutable process configuration for the MCP application.

The compiled sidecar installs exactly one configuration before importing the
large server module. Runtime modules read this object instead of deriving
authority-bearing paths from the current directory or a project ``.env``.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from threading import RLock

from pyocd_debug_mcp import __version__

_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")
_CONFIG_GUARD = RLock()
_CONFIG: ServerApplicationConfig | None = None


@dataclass(frozen=True, slots=True)
class EnvironmentPolicy:
    """The environment contract already enforced by the Rust launcher."""

    inherited_names: tuple[str, ...]
    project_dotenv_allowed: bool = False


@dataclass(frozen=True, slots=True)
class ServerApplicationConfig:
    project_root: Path
    runtime_root: Path
    sidecar_executable: Path
    provider_worker_argv: tuple[str, ...]
    runs_root: Path
    environment_policy: EnvironmentPolicy
    build_version: str
    launcher_version: str
    sidecar_protocol: int
    workflow_protocol: int
    worker_protocol: int
    capsule_schema: int
    project_state_schema: int

    def validated(self) -> ServerApplicationConfig:
        project = _existing_directory(self.project_root, "project root")
        runtime = _existing_directory(self.runtime_root, "runtime root")
        executable = self.sidecar_executable.expanduser().resolve(strict=True)
        if not executable.is_file():
            raise ValueError("sidecar executable must resolve to a regular file")
        worker_argv = tuple(self.provider_worker_argv)
        if (
            not worker_argv
            or any(not isinstance(item, str) or not item or "\0" in item for item in worker_argv)
            or not Path(worker_argv[0]).is_absolute()
        ):
            raise ValueError(
                "provider worker argv must use an absolute executable and explicit arguments"
            )
        runs = self.runs_root.expanduser().resolve(strict=False)
        expected_runs = (project / ".firm" / "runs").resolve(strict=False)
        if runs != expected_runs:
            raise ValueError("runs root must be the project-local .firm/runs directory")
        for label, value in (
            ("build version", self.build_version),
            ("launcher version", self.launcher_version),
        ):
            if not _VERSION.fullmatch(value):
                raise ValueError(f"{label} is not a supported semantic version")
        for label, value in (
            ("sidecar protocol", self.sidecar_protocol),
            ("workflow protocol", self.workflow_protocol),
            ("worker protocol", self.worker_protocol),
            ("capsule schema", self.capsule_schema),
            ("project state schema", self.project_state_schema),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{label} must be a positive integer")
        if self.environment_policy.project_dotenv_allowed:
            raise ValueError("project .env loading is forbidden for the compiled server")
        return ServerApplicationConfig(
            project_root=project,
            runtime_root=runtime,
            sidecar_executable=executable,
            provider_worker_argv=worker_argv,
            runs_root=runs,
            environment_policy=self.environment_policy,
            build_version=self.build_version,
            launcher_version=self.launcher_version,
            sidecar_protocol=self.sidecar_protocol,
            workflow_protocol=self.workflow_protocol,
            worker_protocol=self.worker_protocol,
            capsule_schema=self.capsule_schema,
            project_state_schema=self.project_state_schema,
        )


def _existing_directory(path: Path, label: str) -> Path:
    resolved = path.expanduser().resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError(f"{label} must resolve to a directory")
    return resolved


def install_application_config(config: ServerApplicationConfig) -> ServerApplicationConfig:
    """Install one validated immutable configuration for this process."""

    validated = config.validated()
    global _CONFIG
    with _CONFIG_GUARD:
        if _CONFIG is not None and _CONFIG != validated:
            raise RuntimeError("server application configuration is already installed")
        _CONFIG = validated
    return validated


def application_config() -> ServerApplicationConfig:
    """Return the installed configuration.

    Direct source imports receive an isolated, non-authoritative test root so
    the existing pure unit suite can import server functions. A Nuitka process
    must always be configured by the sidecar dispatcher and fails closed.
    """

    with _CONFIG_GUARD:
        if _CONFIG is not None:
            return _CONFIG
    if _is_compiled():
        raise RuntimeError("compiled server imported before explicit sidecar configuration")
    root = Path(tempfile.gettempdir()) / f"byo-source-application-{os.getpid()}"
    project = root / "project"
    runtime = root / "runtime"
    project.mkdir(parents=True, exist_ok=True)
    runtime.mkdir(parents=True, exist_ok=True)
    fallback = ServerApplicationConfig(
        project_root=project,
        runtime_root=runtime,
        sidecar_executable=Path(sys.executable),
        provider_worker_argv=(
            sys.executable,
            "-m",
            "pyocd_debug_mcp.sidecar",
            "provider-worker",
        ),
        runs_root=project / ".firm" / "runs",
        environment_policy=EnvironmentPolicy(inherited_names=()),
        build_version=__version__,
        launcher_version=__version__,
        sidecar_protocol=1,
        workflow_protocol=1,
        worker_protocol=1,
        capsule_schema=1,
        project_state_schema=1,
    )
    return install_application_config(fallback)


def _is_compiled() -> bool:
    return bool(
        getattr(sys, "frozen", False)
        or "__compiled__" in globals()
        or os.environ.get("BYO_SIDECAR_COMPILED") == "1"
    )


def reset_application_config_for_testing() -> None:
    global _CONFIG
    with _CONFIG_GUARD:
        _CONFIG = None
