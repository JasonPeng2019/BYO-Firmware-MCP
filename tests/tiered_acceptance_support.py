"""Shared black-box helpers for the tiered-capability acceptance contracts.

The helpers deliberately do not construct a replacement policy or authorize a
fake operator.  They isolate project roots, normalise the actual FastMCP public
responses, and provide deterministic child-process boundaries for the crash
contracts.  Individual acceptance modules own the expected behavior.
"""

from __future__ import annotations

import asyncio
import copy
import contextlib
import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Generator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch

from pyocd_debug_mcp.adapters.swd_interface import TargetSessionHandle


SERVER_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = SERVER_ROOT / "src"
FIXTURE_ROOT = Path(__file__).with_name("tiered_acceptance_fixtures")
MANUAL_PERMISSION_RELATIVE = Path(".agent-workspace") / "runtime" / "manual-permissions"
MONITOR_RELATIVE = Path(".agent-workspace") / "runtime" / "acceptance-monitor"


def fixture(name: str) -> dict[str, Any]:
    """Read one declarative fixture, refusing accidental fixture drift."""

    path = FIXTURE_ROOT / name
    with path.open(encoding="utf-8") as handle:
        document = json.load(handle)
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise AssertionError(f"invalid tiered acceptance fixture: {path}")
    return document


def write_json(path: Path, payload: Mapping[str, Any]) -> Path:
    """Write fixture state explicitly, never through the server under test."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(dict(payload), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def seed_legacy_state(project_root: Path, state_name: str) -> dict[str, Any]:
    """Seed a pre-generation legacy state from the migration fixture.

    JSON is valid YAML, so this creates the old profile/map shapes without
    importing today's profile or safety writers (which would accidentally
    exercise the implementation under test while building its input).
    """

    document = fixture("legacy_states.json")
    state = document[state_name]
    if not isinstance(state, dict):
        raise AssertionError(f"unknown legacy state fixture: {state_name}")
    board_id = document["board_id"]
    if not isinstance(board_id, str):
        raise AssertionError("legacy fixture board_id must be a string")
    profile = state.get("profile")
    if not isinstance(profile, dict):
        raise AssertionError(f"legacy fixture {state_name} has no profile")
    write_json(project_root / ".firm" / "boards" / f"{board_id}.json", profile)
    if state_name == "ambiguous_provenance":
        write_json(
            project_root / ".firm" / "boards" / f"{board_id}.yaml",
            {**profile, "board_id": "other_board"},
        )
    return state


def known_good_full_policy() -> tuple[dict[str, Any], dict[str, Any]]:
    """Return immutable fixture copies for the one schema-v2 full-policy baseline.

    The fixture is handwritten historical profile/map evidence, not emitted by
    any implementation writer.  Tests that need a full tier deliberately use
    its one matching board identity instead of inventing ``{kind: ram}``
    placeholders which would normalize an invalid full policy.
    """

    document = fixture("legacy_full_v2.json")
    profile = document.get("profile")
    memory_map = document.get("memory_map")
    if not isinstance(profile, dict) or not isinstance(memory_map, dict):
        raise AssertionError("legacy full fixture lacks profile/map objects")
    if profile.get("board_id") != "legacy_full" or memory_map.get("board_id") != "legacy_full":
        raise AssertionError("legacy full fixture must keep profile/map identity aligned")
    return copy.deepcopy(profile), copy.deepcopy(memory_map)


def confirmed_lite_policy(board_id: str) -> dict[str, Any]:
    """Return a non-empty operator-confirmed lite map for a named test board."""

    confirmation = fixture("lite_confirmation.json")
    regions = confirmation.get("regions")
    flash = confirmation.get("flash")
    if not isinstance(regions, list) or not isinstance(flash, dict):
        raise AssertionError("lite confirmation fixture lacks regions/flash")
    return {"board_id": board_id, "regions": copy.deepcopy(regions), "flash": copy.deepcopy(flash)}


@contextlib.contextmanager
def isolated_project() -> Generator[Path, None, None]:
    """Give a test a private artifact root before its server import boundary."""

    with tempfile.TemporaryDirectory(prefix="tiered-acceptance-") as temporary:
        yield Path(temporary).resolve()


@contextlib.contextmanager
def server_environment(project_root: Path, **extra: str) -> Generator[None, None, None]:
    """Temporarily select the only project root a server process may use."""

    resolved_root = project_root.resolve()
    environment = {
        "BYO_MCP_ARTIFACT_ROOT": str(resolved_root),
        "BYO_MCP_TEST_FAULTS": "1",
        **extra,
        # Monitoring resolves its store while the server imports.  Never let
        # acceptance error paths inherit an operator's platform-data root.
        "BYO_MCP_MONITOR_ROOT": str(resolved_root / MONITOR_RELATIVE),
    }
    with patch.dict(os.environ, environment, clear=False):
        yield


@contextlib.contextmanager
def tiered_stdio_rehearsal_environment(
    project_root: Path, proof_path: Path
) -> Generator[dict[str, str], None, None]:
    """Prepare one child-only fake backend before its module entrypoint runs.

    ``configure_tiered_test_seams`` is intentionally an in-process seam.  This
    focused rehearsal uses Python's standard ``sitecustomize`` import point to
    call it in the child before ``python -m pyocd_debug_mcp.server`` executes.
    The injected backend writes only operation names to a project-local proof
    stream, so the test can prove ordering without a physical probe or a
    general runtime configuration mechanism.
    """

    resolved_root = project_root.resolve()
    resolved_proof = proof_path.resolve()
    try:
        resolved_proof.relative_to(resolved_root)
    except ValueError as exc:
        raise AssertionError("stdio rehearsal proof must stay inside its project root") from exc

    with tempfile.TemporaryDirectory(prefix="tiered-stdio-bootstrap-") as temporary:
        bootstrap_root = Path(temporary).resolve()
        bootstrap_root.joinpath("sitecustomize.py").write_text(
            _tiered_stdio_bootstrap_source(resolved_proof), encoding="utf-8"
        )
        inherited_path = os.environ.get("PYTHONPATH", "")
        python_path = str(bootstrap_root)
        if inherited_path:
            python_path = python_path + os.pathsep + inherited_path
        yield {
            "BYO_MCP_ARTIFACT_ROOT": str(resolved_root),
            "BYO_MCP_MONITOR_ROOT": str(resolved_root / MONITOR_RELATIVE),
            "BYO_MCP_TEST_FAULTS": "1",
            "BYO_MCP_TEST_SEAMS": "1",
            "PYTHONPATH": python_path,
        }


def _tiered_stdio_bootstrap_source(proof_path: Path) -> str:
    """Return a child-local site hook with the smallest normal backend surface."""

    serialized_proof = json.dumps(str(proof_path))
    return f"""from __future__ import annotations

import json
from pathlib import Path

from pyocd_debug_mcp.adapters.swd_interface import TargetSessionHandle

_PROOF_PATH = Path({serialized_proof})


def _record(event: str) -> None:
    _PROOF_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _PROOF_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({{"event": event}}, sort_keys=True) + "\\n")


class _RehearsalBackend:
    def open(self, **kwargs: object) -> TargetSessionHandle:
        _record("backend-open")
        return TargetSessionHandle(
            session=object(),
            board=kwargs.get("board"),
            probe_uid=None,
            route_used="tiered-stdio-rehearsal",
            target_override=kwargs.get("target"),
        )

    def close(self, _handle: TargetSessionHandle) -> None:
        _record("backend-close")

    def get_state(self, _handle: TargetSessionHandle) -> str:
        return "halted"

    def read_memory(
        self,
        _handle: TargetSessionHandle,
        _address: int,
        _width_bits: int,
        **_kwargs: object,
    ) -> int:
        _record("backend-read-memory")
        return 0x12345678

    def read_memory_block(
        self, _handle: TargetSessionHandle, _address: int, length: int
    ) -> list[int]:
        _record("backend-read-memory-block")
        return list(range(length))

    def release_reset(self, _handle: TargetSessionHandle) -> None:
        _record("backend-release-reset")


from pyocd_debug_mcp import server as _server

_server.configure_tiered_test_seams(backend=_RehearsalBackend())
_record("seam-installed")
"""


def fresh_server(project_root: Path):  # type: ignore[no-untyped-def]
    """Import a new server instance after selecting ``project_root``.

    Test modules must call this before touching ``pyocd_debug_mcp.server``.  It
    discards only that module because its root and run-local objects are made at
    import time; lower-level modules hold no artifact-root singleton.
    """

    from pyocd_debug_mcp.monitor import paths as monitor_paths

    with server_environment(project_root):
        monitor_paths._reset_cache()  # noqa: SLF001 - import-time fixture isolation
        sys.modules.pop("pyocd_debug_mcp.server", None)
        return importlib.import_module("pyocd_debug_mcp.server")


@contextlib.contextmanager
def tiered_test_server(project_root: Path) -> Generator[Any, None, None]:
    """Load an isolated server and restore its test seam before the next test.

    ``configure_tiered_test_seams`` deliberately swaps the process-global
    target-control backend.  The server module is disposable per project root,
    but ``target_control`` is not; leaving a deterministic fake installed
    changes later baseline tests in the same interpreter.  Always restore the
    exact prior backend, including when the caller exits with an exception, and
    discard the context-local server module so subsequent ordinary imports do
    not retain its seam-specific globals.
    """

    from pyocd_debug_mcp.services import target_control
    from pyocd_debug_mcp.monitor import paths as monitor_paths

    original_backend = target_control._BACKEND  # noqa: SLF001 - fixture isolation boundary
    original_monitor_cache = (
        monitor_paths._cached_root,  # noqa: SLF001 - fixture isolation boundary
        monitor_paths._cached_salt,  # noqa: SLF001 - fixture isolation boundary
        monitor_paths._override_root,  # noqa: SLF001 - fixture isolation boundary
    )
    monitor_root = project_root.resolve() / MONITOR_RELATIVE
    try:
        with server_environment(project_root, BYO_MCP_TEST_SEAMS="1"):
            monitor_paths._reset_cache()  # noqa: SLF001 - import-time fixture isolation
            sys.modules.pop("pyocd_debug_mcp.server", None)
            yield importlib.import_module("pyocd_debug_mcp.server")
    finally:
        target_control.configure_backend_for_tests(original_backend)
        monitor_paths._reset_cache()  # noqa: SLF001 - do not retain a disposable root
        (
            monitor_paths._cached_root,  # noqa: SLF001 - fixture isolation boundary
            monitor_paths._cached_salt,  # noqa: SLF001 - fixture isolation boundary
            monitor_paths._override_root,  # noqa: SLF001 - fixture isolation boundary
        ) = original_monitor_cache
        shutil.rmtree(monitor_root, ignore_errors=True)
        discarded = sys.modules.pop("pyocd_debug_mcp.server", None)
        package = sys.modules.get("pyocd_debug_mcp")
        if package is not None and getattr(package, "server", None) is discarded:
            delattr(package, "server")


class DeterministicTargetBackend:
    """Normal target-control backend surface with observable, no-hardware calls."""

    def __init__(
        self,
        *,
        read_value: int = 0x12345678,
        read_error: Exception | None = None,
        open_error: Exception | None = None,
        supported_targets: frozenset[str] | None = None,
        recovery_supported: bool = False,
        recover_error: Exception | None = None,
    ) -> None:
        self.read_value = read_value
        self.read_error = read_error
        self.open_error = open_error
        self.supported_targets = supported_targets
        self.recovery_supported = recovery_supported
        self.recover_error = recover_error
        self.calls: list[tuple[object, ...]] = []

    def open(self, **kwargs: Any) -> TargetSessionHandle:
        self.calls.append(("open", kwargs))
        if self.open_error is not None:
            raise self.open_error
        target = kwargs.get("target")
        if self.supported_targets is not None and target not in self.supported_targets:
            raise RuntimeError(f"unsupported target fixture refusal: {target!r}")
        return TargetSessionHandle(
            session=object(),
            board=kwargs["board"],
            probe_uid=kwargs.get("unique_id"),
            route_used="tiered-acceptance-fake",
            target_override=kwargs.get("target"),
        )

    def close(self, handle: TargetSessionHandle) -> None:
        self.calls.append(("close", handle.probe_uid))

    def connect_under_reset(self, **kwargs: Any) -> TargetSessionHandle:
        self.calls.append(("connect_under_reset", kwargs))
        return self.open(**kwargs)

    def get_state(self, handle: TargetSessionHandle) -> str:
        self.calls.append(("get_state", handle.probe_uid))
        return "halted"

    def read_memory(
        self,
        handle: TargetSessionHandle,
        address: int,
        width_bits: int,
        **kwargs: Any,
    ) -> int:
        self.calls.append(("read_memory", handle.probe_uid, address, width_bits))
        if self.read_error is not None:
            raise self.read_error
        return self.read_value

    def read_memory_block(
        self, handle: TargetSessionHandle, address: int, length: int
    ) -> list[int]:
        self.calls.append(("read_memory_block", handle.probe_uid, address, length))
        if self.read_error is not None:
            raise self.read_error
        return list(range(length))

    def write_memory(
        self, handle: TargetSessionHandle, address: int, value: int, width_bits: int
    ) -> None:
        self.calls.append(("write_memory", handle.probe_uid, address, value, width_bits))

    def read_core_register(self, handle: TargetSessionHandle, name: str) -> int:
        self.calls.append(("read_core_register", handle.probe_uid, name))
        return 0

    def write_core_register(self, handle: TargetSessionHandle, name: str, value: int) -> None:
        self.calls.append(("write_core_register", handle.probe_uid, name, value))

    def supported_core_registers(self, handle: TargetSessionHandle) -> tuple[str, ...]:
        self.calls.append(("supported_core_registers", handle.probe_uid))
        return ("r0", "pc", "xpsr")

    def halt(self, handle: TargetSessionHandle) -> None:
        self.calls.append(("halt", handle.probe_uid))

    def resume(self, handle: TargetSessionHandle) -> None:
        self.calls.append(("resume", handle.probe_uid))

    def step(self, handle: TargetSessionHandle) -> None:
        self.calls.append(("step", handle.probe_uid))

    def reset(self, handle: TargetSessionHandle) -> None:
        self.calls.append(("reset", handle.probe_uid))

    def reset_and_halt(self, handle: TargetSessionHandle) -> None:
        self.calls.append(("reset_and_halt", handle.probe_uid))

    def release_reset(self, handle: TargetSessionHandle) -> None:
        self.calls.append(("release_reset", handle.probe_uid))

    def flash(self, handle: TargetSessionHandle, firmware: Path, *, halt_after_reset: bool) -> str:
        self.calls.append(("flash", handle.probe_uid, firmware, halt_after_reset))
        return "halted"

    def recover(self, handle: TargetSessionHandle) -> None:
        self.calls.append(("recover", handle.probe_uid))
        if self.recover_error is not None:
            raise self.recover_error

    def supports_recovery(self, handle: TargetSessionHandle, mechanism: str) -> bool:
        self.calls.append(("supports_recovery", handle.probe_uid, mechanism))
        return self.recovery_supported

    def set_breakpoint(self, handle: TargetSessionHandle, address: int) -> None:
        self.calls.append(("set_breakpoint", handle.probe_uid, address))

    def remove_breakpoint(self, handle: TargetSessionHandle, address: int) -> None:
        self.calls.append(("remove_breakpoint", handle.probe_uid, address))


def _content_text(value: Any) -> str:
    if isinstance(value, tuple):
        value = value[0]
    if isinstance(value, list):
        if len(value) != 1:
            raise AssertionError(f"expected one MCP text content item, got {value!r}")
        value = value[0]
    text = getattr(value, "text", value)
    if not isinstance(text, str):
        raise AssertionError(f"expected MCP text result, got {type(text).__name__}: {text!r}")
    return text


def parse_response(value: Any) -> dict[str, Any]:
    """Parse the versioned JSON envelope returned by a new public tool."""

    text = _content_text(value)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AssertionError(f"tiered public response was not JSON: {text!r}") from exc
    if not isinstance(payload, dict):
        raise AssertionError(f"tiered public response was not an object: {payload!r}")
    if payload.get("schema_version") != 1:
        raise AssertionError(f"tiered public response lacks schema_version=1: {payload!r}")
    return payload


def public_call(server: Any, tool_name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Execute one registered public tool, not a private implementation helper."""

    try:
        response = asyncio.run(server.mcp.call_tool(tool_name, dict(arguments)))
    except Exception as exc:  # FastMCP exposes expected refusals as tool errors.
        return parse_response(str(exc))
    return parse_response(response)


def public_text(server: Any, tool_name: str, arguments: Mapping[str, Any]) -> str:
    """Execute a baseline-text tool through the same public MCP dispatcher."""

    return _content_text(asyncio.run(server.mcp.call_tool(tool_name, dict(arguments))))


def assert_refusal(
    testcase: Any,
    payload: Mapping[str, Any],
    code: str,
    *,
    operation: str | None = None,
) -> None:
    testcase.assertEqual(payload.get("status"), "refused", payload)
    testcase.assertEqual(payload.get("code"), code, payload)
    if operation is not None:
        testcase.assertEqual(payload.get("operation"), operation, payload)
    remedies = payload.get("remedies")
    testcase.assertIsInstance(remedies, list, payload)


def assert_lite_raw_warning(testcase: Any, payload: Mapping[str, Any], operation: str) -> None:
    testcase.assertEqual(payload.get("tier"), "setup-lite", payload)
    testcase.assertTrue(payload.get("raw"), payload)
    testcase.assertEqual(payload.get("operation"), operation, payload)
    warning = payload.get("warning")
    testcase.assertIsInstance(warning, dict, payload)
    testcase.assertEqual(warning.get("code"), "tier/lite-containment-bypassed", payload)
    testcase.assertTrue(warning.get("display_to_human"), payload)


def manual_permission_root(project_root: Path) -> Path:
    return project_root / MANUAL_PERMISSION_RELATIVE


def assert_manual_record(
    testcase: Any,
    path: Path,
    *,
    action: str,
    state: str,
    project_root: Path,
) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    testcase.assertEqual(payload.get("schema_version"), 1, payload)
    testcase.assertEqual(payload.get("action"), action, payload)
    testcase.assertEqual(payload.get("state"), state, payload)
    testcase.assertTrue(payload.get("server_run_id"), payload)
    testcase.assertTrue(str(path.resolve()).startswith(str(project_root.resolve())), path)
    return payload


@dataclass(frozen=True)
class ChildResult:
    returncode: int
    stdout: str
    stderr: str

    def json_stdout(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.stdout)
        except json.JSONDecodeError as exc:
            raise AssertionError(
                f"child did not produce exactly one JSON payload: {self.stdout!r}\n{self.stderr}"
            ) from exc
        if not isinstance(payload, dict):
            raise AssertionError(f"child payload was not an object: {payload!r}")
        return payload


def run_child(
    project_root: Path, script: str, *, extra_env: Mapping[str, str] | None = None
) -> ChildResult:
    """Run a hermetic Python child with an explicit root and no hardware access."""

    environment = dict(os.environ)
    source_path = str(SOURCE_ROOT)
    environment["PYTHONPATH"] = source_path + os.pathsep + environment.get("PYTHONPATH", "")
    environment["BYO_MCP_ARTIFACT_ROOT"] = str(project_root.resolve())
    environment["BYO_MCP_TEST_FAULTS"] = "1"
    environment["PYTHONUNBUFFERED"] = "1"
    if extra_env:
        environment.update(extra_env)
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=SERVER_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    return ChildResult(completed.returncode, completed.stdout.strip(), completed.stderr.strip())


def assert_no_backend_calls(testcase: Any, backend: Any) -> None:
    """Keep tier refusals above backend I/O, even when a fake is available."""

    calls = getattr(backend, "calls", None)
    testcase.assertEqual(calls, [], f"forbidden backend call(s): {calls!r}")
