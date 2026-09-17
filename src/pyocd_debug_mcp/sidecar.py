"""Multicall entry point for the packaged BYO firmware MCP sidecar."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Callable, NoReturn, Sequence

from pyocd_debug_mcp import __version__

SIDECAR_PROTOCOL = 1
WORKER_PROTOCOL = 1
WORKFLOW_PROTOCOL = 1
CAPSULE_SCHEMA = 1
PROJECT_STATE_SCHEMA = 1


class ConfigurationError(ValueError):
    """A sidecar path or protocol argument failed closed validation."""


@dataclass(frozen=True, slots=True)
class RuntimeContract:
    version: str
    sidecar_protocol: int
    worker_protocol: int
    workflow_protocol: int
    capsule_schema: int
    project_state_schema: int


@dataclass(frozen=True, slots=True)
class RuntimeContext:
    """Validated runtime selection owned by the launcher invocation."""

    project_root: Path
    runtime_root: Path
    contract: RuntimeContract
    launcher_version: str


def _canonical_directory(raw: str, label: str) -> Path:
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise ConfigurationError(f"{label} must be an absolute path")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise ConfigurationError(f"{label} does not resolve to an existing directory") from error
    if not resolved.is_dir():
        raise ConfigurationError(f"{label} must be a directory")
    return resolved


def validate_project_root(raw: str) -> Path:
    """Return the explicit, canonical project root supplied by the launcher."""

    project = _canonical_directory(raw, "project root")
    if project == Path(project.anchor).resolve() or project == Path.home().resolve():
        raise ConfigurationError("project root must not be a filesystem root or home directory")
    return project


def _is_compiled() -> bool:
    return bool(
        getattr(sys, "frozen", False)
        or "__compiled__" in globals()
        or os.environ.get("BYO_SIDECAR_COMPILED") == "1"
    )


def _running_sidecar() -> Path:
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


def _source_worker_base_argv() -> tuple[str, ...]:
    """Preserve the active development environment for a source worker."""

    return (sys.executable, "-m", "pyocd_debug_mcp.sidecar", "provider-worker")


def _provider_worker_argv(
    project_root: Path,
    runtime_root: Path,
    launcher_version: str,
    workflow_protocol: int,
) -> tuple[str, ...]:
    """Launch workers through this multicall sidecar with trusted context."""

    base = (
        (str(_running_sidecar()), "provider-worker")
        if _is_compiled()
        else _source_worker_base_argv()
    )
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


def _load_runtime_contract(runtime_root: Path) -> RuntimeContract:
    try:
        payload = json.loads((runtime_root / "release-manifest.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ConfigurationError("runtime release manifest is missing or invalid") from error
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != 1
        or payload.get("product") != "byo"
    ):
        raise ConfigurationError("runtime release manifest has an unsupported identity")
    required = {
        "version": str,
        "sidecar_protocol": int,
        "worker_protocol": int,
        "workflow_protocol": int,
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
        worker_protocol=payload["worker_protocol"],
        workflow_protocol=payload["workflow_protocol"],
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
        worker_protocol=WORKER_PROTOCOL,
        workflow_protocol=WORKFLOW_PROTOCOL,
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
            f"workflow protocol {workflow_protocol} is unsupported; expected {contract.workflow_protocol}"
        )


def _validate_project_capsule(project_root: Path, contract: RuntimeContract) -> None:
    try:
        capsule = json.loads(
            (project_root / ".agent-workspace" / "manifest.json").read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ConfigurationError("project capsule manifest is missing or invalid") from error
    if (
        not isinstance(capsule, dict)
        or capsule.get("schema") != contract.capsule_schema
        or capsule.get("product") != "byo"
        or capsule.get("workflow_protocol") != contract.workflow_protocol
    ):
        raise ConfigurationError("project capsule is incompatible with the runtime")


def _install_runtime_context(args: argparse.Namespace, *, require_capsule: bool) -> RuntimeContext:
    project_root = validate_project_root(args.project_root)
    runtime_root = _canonical_directory(args.runtime_root, "runtime root")
    contract = _load_runtime_contract(runtime_root)
    _validate_contract(
        contract,
        launcher_version=args.launcher_version,
        workflow_protocol=args.workflow_protocol,
    )
    if require_capsule:
        _validate_project_capsule(project_root, contract)
    # The current server resolves project-local state during module import.
    # Replace, rather than trust, any ambient roots before those imports happen.
    for name in (
        "BYO_MCP_ARTIFACT_ROOT",
        "BYO_SIDECAR_EXECUTABLE",
        "BYO_RUNTIME_ROOT",
        "BYO_PROVIDER_WORKER_ARGV",
        "PYOCD_MCP_RUNS_ROOT",
    ):
        os.environ.pop(name, None)
    os.environ["BYO_MCP_ARTIFACT_ROOT"] = str(project_root)
    os.environ["PYOCD_MCP_RUNS_ROOT"] = str(project_root / ".firm" / "runs")
    # `server.py` still has development `.env` discovery at import time. Its
    # cwd must be a validated runtime directory, never the operator's project.
    os.chdir(runtime_root)
    return RuntimeContext(
        project_root=project_root,
        runtime_root=runtime_root,
        contract=contract,
        launcher_version=args.launcher_version,
    )


def _serve(args: argparse.Namespace) -> int:
    context = _install_runtime_context(args, require_capsule=True)
    from pyocd_debug_mcp.adapters.swd_process import ProcessIsolatedSWDInterface
    from pyocd_debug_mcp.services.target_control import configure_backend_for_runtime

    configure_backend_for_runtime(
        ProcessIsolatedSWDInterface(
            worker_argv=_provider_worker_argv(
                context.project_root,
                context.runtime_root,
                context.launcher_version,
                context.contract.workflow_protocol,
            )
        )
    )
    from pyocd_debug_mcp.server import main as run_server

    run_server()
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
    if arguments[:1] == ("--",):
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


def _manual_permission(args: argparse.Namespace) -> int:
    context = _install_runtime_context(args, require_capsule=True)
    from pyocd_debug_mcp.capabilities.manual_permission_helper import unlock_manual_grant

    result = unlock_manual_grant(
        project_root=context.project_root,
        action=args.action,
        board_id=args.board_id,
        policy_digest=args.policy_digest,
        binding_digest=args.binding_digest,
    )
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if result["status"] == "manual_grant_unlocked" else 1


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
    elif _is_compiled():
        raise ConfigurationError("compiled self-test requires --runtime-root")

    temporary_project: tempfile.TemporaryDirectory[str] | None = None
    if contract is not None:
        temporary_project = tempfile.TemporaryDirectory(prefix="byo-sidecar-self-test-")
        project_root = Path(temporary_project.name).resolve()
        worker_argv = _provider_worker_argv(
            project_root,
            runtime_root,
            args.launcher_version or __version__,
            contract.workflow_protocol,
        )
    else:
        worker_argv = _source_worker_base_argv()

    # Exercise imported native modules and package data that a standalone
    # build must carry, then prove its worker protocol can actually start.
    import cmsis_pack_manager  # type: ignore[import-not-found]  # noqa: F401
    import libusb_package  # type: ignore[import-not-found]  # noqa: F401
    import pyocd  # type: ignore[import-not-found]  # noqa: F401
    from pyocd_debug_mcp.probe_families import load_probe_family_registry

    try:
        package_version = metadata.version("pyocd-debug-mcp")
    except metadata.PackageNotFoundError:
        package_version = __version__
    if package_version != __version__:
        raise RuntimeError("bundled package metadata version does not match the sidecar")
    if not load_probe_family_registry():
        raise RuntimeError("packaged probe family registry was empty")

    process: subprocess.Popen[str] | None = None
    diagnostics = ""
    try:
        process = subprocess.Popen(
            worker_argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert process.stdout is not None
        ready = process.stdout.readline()
        if json.loads(ready) != {"version": WORKER_PROTOCOL, "ready": True}:
            raise RuntimeError("provider worker returned an invalid handshake")
    finally:
        try:
            if process is not None:
                if process.stdin is not None:
                    process.stdin.close()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired as error:
                    process.kill()
                    process.wait(timeout=5)
                    raise RuntimeError("provider worker did not stop after protocol EOF") from error
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    diagnostics = process.stderr.read()
                    process.stderr.close()
        finally:
            if temporary_project is not None:
                temporary_project.cleanup()
    if process is None or process.returncode != 0:
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


def _add_runtime_context_arguments(parser: argparse.ArgumentParser, *, required: bool) -> None:
    parser.add_argument("--project-root", required=required)
    parser.add_argument("--runtime-root", required=required)
    parser.add_argument("--launcher-version", required=required)
    parser.add_argument("--workflow-protocol", required=required, type=int)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="byo-mcp-sidecar")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)

    serve = commands.add_parser("serve", help="Run MCP over stdio")
    _add_runtime_context_arguments(serve, required=True)
    serve.set_defaults(handler=_serve)

    worker = commands.add_parser("provider-worker", help=argparse.SUPPRESS)
    _add_runtime_context_arguments(worker, required=False)
    worker.set_defaults(handler=_provider_worker)

    for name in ("collect-artifacts", "native-build", "pack-repair"):
        helper = commands.add_parser(name, help=argparse.SUPPRESS)
        _add_runtime_context_arguments(helper, required=True)
        helper.add_argument("arguments", nargs=argparse.REMAINDER)
        helper.set_defaults(handler=_helper)

    manual = commands.add_parser("manual-permission", help=argparse.SUPPRESS)
    _add_runtime_context_arguments(manual, required=True)
    manual.add_argument("--action", required=True, choices=("downgrade", "mass-erase"))
    manual.add_argument("--board-id", required=True)
    manual.add_argument("--policy-digest", required=True)
    manual.add_argument("--binding-digest")
    manual.set_defaults(handler=_manual_permission)

    self_test = commands.add_parser("self-test", help="Run hardware-free packaged checks")
    self_test.add_argument("--runtime-root")
    self_test.add_argument("--launcher-version")
    self_test.set_defaults(handler=_self_test)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        return int(args.handler(args))
    except ConfigurationError as error:
        print(f"BYO sidecar configuration error: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


def entrypoint() -> NoReturn:
    raise SystemExit(main())


if __name__ == "__main__":
    entrypoint()
