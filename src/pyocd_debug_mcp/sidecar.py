"""Multicall entry point for the private BYO firmware MCP sidecar.

Production packaging exposes this module only through the private
``byo-mcp-sidecar`` executable.  The public ``byo`` launcher supplies every
authority-bearing path explicitly.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from importlib import metadata
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import NoReturn

from pyocd_debug_mcp import __version__
from pyocd_debug_mcp.application import (
    EnvironmentPolicy,
    ServerApplicationConfig,
    install_application_config,
)

SIDECAR_PROTOCOL = 1
WORKER_PROTOCOL = 1
WORKFLOW_PROTOCOL = 1
CAPSULE_SCHEMA = 1
PROJECT_STATE_SCHEMA = 1
_INHERITED_ENVIRONMENT = (
    "HOME",
    "TMPDIR",
    "TEMP",
    "TMP",
    "LANG",
    "LC_ALL",
    "PATH",
    "SystemRoot",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
)


class ConfigurationError(ValueError):
    """A sidecar path or protocol argument failed closed validation."""


@dataclass(frozen=True, slots=True)
class RuntimeContract:
    version: str
    sidecar_protocol: int
    workflow_protocol: int
    worker_protocol: int
    capsule_schema: int
    project_state_schema: int


def _canonical_directory(raw: str, label: str) -> Path:
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise ConfigurationError(f"{label} must be an absolute path")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ConfigurationError(f"{label} does not resolve to an existing directory") from exc
    if not resolved.is_dir():
        raise ConfigurationError(f"{label} must be a directory")
    return resolved


def validate_project_root(raw: str) -> Path:
    """Return a canonical, bounded project directory."""

    project = _canonical_directory(raw, "project root")
    anchor = Path(project.anchor).resolve()
    home = Path.home().resolve()
    if project == anchor:
        raise ConfigurationError("project root must not be a filesystem root")
    if project == home:
        raise ConfigurationError("project root must not be the user's home directory")
    return project


def _running_sidecar() -> Path:
    """Resolve the current executable without trusting PATH."""

    # Nuitka standalone may retain a synthetic ``sys.executable`` ending in
    # ``sidecar/python``. The invoked argv[0] is the actual compiled multicall
    # binary and is the correct worker-spawn target.
    for raw in (sys.argv[0], sys.executable):
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        try:
            executable = candidate.resolve(strict=True)
        except OSError:
            continue
        if executable.is_file():
            return executable
    raise ConfigurationError("sidecar executable did not resolve to a file")


def _source_worker_base_argv() -> tuple[str, ...]:
    # Preserve a development virtual-environment shim. Resolving that symlink
    # can bypass its environment and import the base interpreter instead.
    return (sys.executable, "-m", "pyocd_debug_mcp.sidecar", "provider-worker")


def _compiled_worker_base_argv() -> tuple[str, ...]:
    return (str(_running_sidecar()), "provider-worker")


def _provider_worker_argv(
    project_root: Path,
    runtime_root: Path,
    launcher_version: str,
    workflow_protocol: int,
) -> tuple[str, ...]:
    base = _compiled_worker_base_argv() if _is_compiled() else _source_worker_base_argv()
    return (
        *base,
        "--project-root",
        str(project_root),
        "--runtime-root",
        str(runtime_root),
        "--launcher-version",
        launcher_version,
        "--workflow-protocol",
        str(workflow_protocol),
    )


def _is_compiled() -> bool:
    return bool(
        getattr(sys, "frozen", False)
        or "__compiled__" in globals()
        or os.environ.get("BYO_SIDECAR_COMPILED") == "1"
    )


def _load_runtime_contract(runtime_root: Path) -> RuntimeContract:
    manifest_path = runtime_root / "release-manifest.json"
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigurationError("runtime release manifest is missing or invalid") from exc
    if not isinstance(payload, dict) or payload.get("schema") != 1 or payload.get("product") != "byo":
        raise ConfigurationError("runtime release manifest has an unsupported identity")
    required = {
        "version": str,
        "sidecar_protocol": int,
        "workflow_protocol": int,
        "worker_protocol": int,
        "capsule_schema": int,
        "project_state_schema": int,
    }
    for name, expected_type in required.items():
        value = payload.get(name)
        if not isinstance(value, expected_type) or isinstance(value, bool):
            raise ConfigurationError(f"runtime release manifest has an invalid {name}")
    return RuntimeContract(
        version=payload["version"],
        sidecar_protocol=payload["sidecar_protocol"],
        workflow_protocol=payload["workflow_protocol"],
        worker_protocol=payload["worker_protocol"],
        capsule_schema=payload["capsule_schema"],
        project_state_schema=payload["project_state_schema"],
    )


def _validate_contract(
    contract: RuntimeContract,
    *,
    launcher_version: str,
    workflow_protocol: int,
) -> None:
    expected = RuntimeContract(
        version=__version__,
        sidecar_protocol=SIDECAR_PROTOCOL,
        workflow_protocol=WORKFLOW_PROTOCOL,
        worker_protocol=WORKER_PROTOCOL,
        capsule_schema=CAPSULE_SCHEMA,
        project_state_schema=PROJECT_STATE_SCHEMA,
    )
    if contract != expected:
        raise ConfigurationError(
            "compiled sidecar version/protocol contract does not match the runtime manifest"
        )
    if launcher_version != contract.version:
        raise ConfigurationError(
            f"launcher version {launcher_version} does not match runtime {contract.version}"
        )
    if workflow_protocol != contract.workflow_protocol:
        raise ConfigurationError(
            f"workflow protocol {workflow_protocol} is unsupported; "
            f"expected {contract.workflow_protocol}"
        )


def _validate_project_capsule(project_root: Path, contract: RuntimeContract) -> None:
    path = project_root / ".agent-workspace" / "manifest.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigurationError("project capsule manifest is missing or invalid") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != contract.capsule_schema
        or payload.get("product") != "byo"
        or payload.get("workflow_protocol") != contract.workflow_protocol
    ):
        raise ConfigurationError("project capsule is incompatible with the runtime")


def _configure_serve_environment() -> None:
    """Discard inbound environment variables that previously carried authority."""

    for name in (
        "BYO_MCP_ARTIFACT_ROOT",
        "BYO_SIDECAR_EXECUTABLE",
        "BYO_RUNTIME_ROOT",
        "BYO_PROVIDER_WORKER_ARGV",
        "PYOCD_MCP_RUNS_ROOT",
    ):
        os.environ.pop(name, None)


def _application_config(
    project_root: Path,
    runtime_root: Path,
    contract: RuntimeContract,
    launcher_version: str,
) -> ServerApplicationConfig:
    executable = _running_sidecar() if _is_compiled() else Path(sys.executable).resolve(strict=True)
    return ServerApplicationConfig(
        project_root=project_root,
        runtime_root=runtime_root,
        sidecar_executable=executable,
        provider_worker_argv=_provider_worker_argv(
            project_root,
            runtime_root,
            launcher_version,
            contract.workflow_protocol,
        ),
        runs_root=project_root / ".firm" / "runs",
        environment_policy=EnvironmentPolicy(inherited_names=_INHERITED_ENVIRONMENT),
        build_version=contract.version,
        launcher_version=launcher_version,
        sidecar_protocol=contract.sidecar_protocol,
        workflow_protocol=contract.workflow_protocol,
        worker_protocol=contract.worker_protocol,
        capsule_schema=contract.capsule_schema,
        project_state_schema=contract.project_state_schema,
    )


def _install_runtime_context(
    args: argparse.Namespace, *, require_capsule: bool
) -> ServerApplicationConfig:
    project_root = validate_project_root(args.project_root)
    runtime_root = _canonical_directory(args.runtime_root, "runtime root")
    if args.workflow_protocol != WORKFLOW_PROTOCOL:
        raise ConfigurationError(
            f"workflow protocol {args.workflow_protocol} is unsupported; "
            f"expected {WORKFLOW_PROTOCOL}"
        )
    contract = _load_runtime_contract(runtime_root)
    _validate_contract(
        contract,
        launcher_version=args.launcher_version,
        workflow_protocol=args.workflow_protocol,
    )
    if require_capsule:
        _validate_project_capsule(project_root, contract)
    _configure_serve_environment()
    return install_application_config(
        _application_config(project_root, runtime_root, contract, args.launcher_version)
    )


def _serve(args: argparse.Namespace) -> int:
    config = _install_runtime_context(args, require_capsule=True)

    # Import only after path and protocol authority has been validated.
    from pyocd_debug_mcp.server import create_server_application

    create_server_application(config).run()
    return 0


def _provider_worker(args: argparse.Namespace) -> int:
    context = (
        args.project_root,
        args.runtime_root,
        args.launcher_version,
        args.workflow_protocol,
    )
    if any(value is None for value in context):
        if _is_compiled():
            raise ConfigurationError("compiled provider worker requires explicit runtime context")
    else:
        _install_runtime_context(args, require_capsule=False)
    from pyocd_debug_mcp.adapters.provider_worker import main as run_worker

    run_worker()
    return 0


def _forward_main(
    module_main: Callable[..., object],
    command: str,
    arguments: Sequence[str],
    *,
    accepts_argv: bool = False,
) -> int:
    if accepts_argv:
        result = module_main(arguments)
    else:
        prior = sys.argv
        try:
            sys.argv = [f"byo-mcp-sidecar {command}", *arguments]
            result = module_main()
        finally:
            sys.argv = prior
    return int(result) if isinstance(result, int) else 0


def _helper(args: argparse.Namespace) -> int:
    _install_runtime_context(args, require_capsule=True)
    arguments = tuple(args.arguments)
    if arguments and arguments[0] == "--":
        arguments = arguments[1:]
    if args.command == "collect-artifacts":
        from pyocd_debug_mcp.artifact_collector import main

        return _forward_main(main, args.command, arguments, accepts_argv=True)
    if args.command == "native-build":
        from pyocd_debug_mcp.native_build import main

        return _forward_main(main, args.command, arguments)
    if args.command == "pack-repair":
        from pyocd_debug_mcp.pack_index_repair import main

        return _forward_main(main, args.command, arguments)
    raise ConfigurationError("unknown helper command")


def _self_test(args: argparse.Namespace) -> int:
    contract: RuntimeContract | None = None
    if args.runtime_root is not None:
        runtime_root = _canonical_directory(args.runtime_root, "runtime root")
        contract = _load_runtime_contract(runtime_root)
        _validate_contract(
            contract,
            launcher_version=args.launcher_version or __version__,
            workflow_protocol=WORKFLOW_PROTOCOL,
        )

    # Exercise imports that carry packaged Python/native/data dependencies.
    import cmsis_pack_manager  # type: ignore[import-not-found]  # noqa: F401
    import libusb_package  # type: ignore[import-not-found]  # noqa: F401
    import pyocd  # type: ignore[import-not-found]  # noqa: F401
    from pyocd_debug_mcp.adapters import provider_worker  # noqa: F401
    from pyocd_debug_mcp.probe_families import load_probe_family_registry

    try:
        package_version = metadata.version("pyocd-debug-mcp")
    except metadata.PackageNotFoundError:
        package_version = __version__
    if package_version != __version__:
        raise RuntimeError("bundled package metadata version does not match the sidecar")

    registry = load_probe_family_registry()
    if not registry:
        raise RuntimeError("packaged probe family registry was empty")

    with tempfile.TemporaryDirectory(prefix="byo-sidecar-self-test-") as raw_root:
        project_root = validate_project_root(raw_root)
        runs_root = project_root / ".firm" / "runs"
        runs_root.mkdir(parents=True)
        marker = runs_root / ".write-test"
        marker.write_text("ok\n", encoding="utf-8")
        marker.unlink()

        if contract is not None:
            assert args.runtime_root is not None
            argv = _provider_worker_argv(
                project_root,
                Path(args.runtime_root).resolve(strict=True),
                args.launcher_version or __version__,
                contract.workflow_protocol,
            )
        else:
            if _is_compiled():
                raise ConfigurationError("compiled self-test requires --runtime-root")
            argv = _source_worker_base_argv()
        process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert process.stdout is not None
            ready = process.stdout.readline()
            if json.loads(ready) != {"version": WORKER_PROTOCOL, "ready": True}:
                raise RuntimeError("provider worker returned an invalid handshake")
        finally:
            if process.stdin is not None:
                process.stdin.close()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
                raise RuntimeError("provider worker did not stop after protocol EOF")
        diagnostics = process.stderr.read() if process.stderr is not None else ""
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()
        if process.returncode != 0:
            raise RuntimeError(f"provider worker self-test failed: {diagnostics.strip()}")

    print(
        json.dumps(
            {
                "schema": 1,
                "status": "passed",
                "version": __version__,
                "sidecar_protocol": SIDECAR_PROTOCOL,
                "worker_protocol": WORKER_PROTOCOL,
                "workflow_protocol": WORKFLOW_PROTOCOL,
                "capsule_schema": CAPSULE_SCHEMA,
                "project_state_schema": PROJECT_STATE_SCHEMA,
                "compiled": _is_compiled(),
                "runtime_manifest_verified": contract is not None,
            },
            sort_keys=True,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="byo-mcp-sidecar")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)

    serve = commands.add_parser("serve", help="Run MCP over stdio")
    serve.add_argument("--project-root", required=True)
    serve.add_argument("--runtime-root", required=True)
    serve.add_argument("--launcher-version", required=True)
    serve.add_argument("--workflow-protocol", required=True, type=int)
    serve.set_defaults(handler=_serve)

    worker = commands.add_parser("provider-worker", help=argparse.SUPPRESS)
    _add_runtime_context_arguments(worker, required=False)
    worker.set_defaults(handler=_provider_worker)

    for name in ("collect-artifacts", "native-build", "pack-repair"):
        helper = commands.add_parser(name, help=argparse.SUPPRESS)
        _add_runtime_context_arguments(helper, required=True)
        helper.add_argument("arguments", nargs=argparse.REMAINDER)
        helper.set_defaults(handler=_helper)

    self_test = commands.add_parser("self-test", help="Run hardware-free packaged checks")
    self_test.add_argument("--runtime-root")
    self_test.add_argument("--launcher-version")
    self_test.set_defaults(handler=_self_test)
    return parser


def _add_runtime_context_arguments(
    parser: argparse.ArgumentParser, *, required: bool
) -> None:
    parser.add_argument("--project-root", required=required)
    parser.add_argument("--runtime-root", required=required)
    parser.add_argument("--launcher-version", required=required)
    parser.add_argument("--workflow-protocol", required=required, type=int)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        return int(args.handler(args))
    except ConfigurationError as exc:
        print(f"BYO sidecar configuration error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


def entrypoint() -> NoReturn:
    raise SystemExit(main())


if __name__ == "__main__":
    entrypoint()
