"""Shared black-box helpers for the independent code-analysis contract tests.

Everything that depends on an implementation-owned signature is isolated here
so a seam change touches one file.  The helpers never construct a replacement
for the behavior under test: they build isolated projects, a scriptable fake
clangd reachable through the real owned-process path, independent Unicode
position oracles and envelope assertions derived from
``docs/code-analysis-contract.md``.

Test-harness executables (the fake peer, or an explicitly supplied real clangd)
reach the service only through constructor injection or the server's
``BYO_MCP_TEST_SEAMS=1`` seam, never through a public tool argument.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import hashlib
import importlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import threading
import time
import unittest
from collections.abc import Callable, Generator, Iterable, Mapping
from pathlib import Path
from typing import Any

SERVER_ROOT = Path(__file__).resolve().parents[1]
FAKE_CLANGD = Path(__file__).resolve().with_name("fake_clangd.py")
FIXTURE_ROOT = Path(__file__).resolve().with_name("code_analysis_fixtures")

# Explicit, test-harness-only real program selection.  Never read by product
# code; tests that need a real clangd skip (recorded as unmet) when it is unset.
REAL_CLANGD_ENV = "BYO_MCP_TEST_CLANGD_EXECUTABLE"

TOOL_OPERATIONS: dict[str, str] = {
    "get_code_analysis_status": "status",
    "find_code_definition": "definition",
    "find_code_references": "references",
    "get_code_hover": "hover",
    "get_code_diagnostics": "diagnostics",
}
ANALYSIS_TOOLS = tuple(TOOL_OPERATIONS)
STATUS_VALUES = {"ok", "not_found", "unavailable", "not_configured", "incomplete", "error"}
FAILURE_STATUSES = {"unavailable", "not_configured", "incomplete", "error"}
SESSION_STATES = {"not_started", "initializing", "ready", "restarting", "failed", "closed"}
ERROR_CODES = {
    f"analysis/{name}"
    for name in (
        "config-invalid",
        "database-missing",
        "database-ambiguous",
        "database-invalid",
        "database-conflict",
        "clangd-config-conflict",
        "driver-invalid",
        "compile-context-missing",
        "executable-unavailable",
        "invalid-argument",
        "timeout",
        "source-changed",
        "configuration-changed",
        "diagnostics-unversioned",
        "diagnostics-missing",
        "capability-unsupported",
        "protocol-error",
        "child-exited",
        "cleanup-failed",
    )
}
COMMON_FIELDS = {
    "status",
    "backend",
    "operation",
    "project_root",
    "compilation_database",
    "compilation_database_sha256",
    "index_completeness",
    "data",
    "warnings",
}
# Contract: one LSP shutdown/exit attempt inside the 5 s grace plus the owned
# group termination reserve (1.5 s).  Scheduler slack is added per assertion.
ARGUMENT_TIMEOUT_GRACE_SECONDS = 5.0
OWNED_CLEANUP_SECONDS = 1.5
SCHEDULING_SLACK_SECONDS = 2.0


# ---------------------------------------------------------------- module seams
def analysis_module(name: str) -> Any:
    """Import a production analysis module; ImportError is an honest RED."""

    return importlib.import_module(f"pyocd_debug_mcp.{name}")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(Path(path).read_bytes())


def field(value: Any, name: str, default: Any = dataclasses.MISSING) -> Any:
    """Read ``name`` from a mapping or an attribute-bearing record."""

    if isinstance(value, Mapping):
        if name in value:
            return value[name]
    elif hasattr(value, name):
        return getattr(value, name)
    if default is dataclasses.MISSING:
        raise AssertionError(f"{type(value).__name__} has no field {name!r}: {value!r}")
    return default


def same_path(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return left is right
    return os.path.normcase(str(Path(left).resolve())) == os.path.normcase(
        str(Path(right).resolve())
    )


def problem_codes(snapshot_or_data: Any, attribute: str = "problems") -> list[str]:
    return [str(field(problem, "code")) for problem in field(snapshot_or_data, attribute)]


def problems_with(snapshot: Any, code: str) -> list[Any]:
    return [problem for problem in field(snapshot, "problems") if field(problem, "code") == code]


def describe(problem: Any) -> str:
    return " ".join(
        str(field(problem, name, "") or "") for name in ("code", "field", "message", "remedy")
    )


def make_context(project_root: Path, *, runtime_root: Path | None = None, managed: bool = False):
    runtime = analysis_module("code_analysis_runtime")
    return runtime.AnalysisRuntimeContext(
        project_root=Path(project_root), runtime_root=runtime_root, managed=managed
    )


def make_program(
    executable: Path | None,
    *,
    version: str | None = "clangd version 23.1.0-fake",
    origin: str | None = "path",
    problems: tuple[Any, ...] = (),
) -> Any:
    """Owner seam (ROOT relay 22d4c696): ResolvedAnalysisProgram(name, executable,
    version, origin, identity=(), data_paths=(), problems=())."""

    runtime = analysis_module("code_analysis_runtime")
    return runtime.ResolvedAnalysisProgram(
        name="clangd",
        executable=None if executable is None else Path(executable).resolve(),
        version=version,
        origin=origin,
        problems=problems,
    )


def fixed_resolver(executable: Path | None, **kwargs: Any) -> Callable[..., Any]:
    """Resolver called as ``resolver(context, name, absolute_monotonic_deadline)``."""

    calls: list[tuple[Any, ...]] = []

    def resolve(context: Any, name: str, deadline: float) -> Any:
        calls.append((context, name, deadline, time.monotonic()))
        return make_program(executable, **kwargs)

    resolve.calls = calls  # type: ignore[attr-defined]
    return resolve


def fake_adapter_factory(fake: "FakeClangd") -> Callable[..., Any]:
    """Private ClangdAdapter subclass whose ``_argv`` launches the fake peer.

    The real adapter keeps its owned pipes, framing, reader and process group;
    only argv[0] is replaced by ``python fake_clangd.py --scenario ...`` so the
    clangd flags the adapter would pass remain visible in the fake's log.
    """

    adapter_module = analysis_module("adapters.clangd")

    class FakeProgramAdapter(adapter_module.ClangdAdapter):
        def _argv(self) -> list[str]:
            real = list(super()._argv())
            return [
                sys.executable,
                str(FAKE_CLANGD),
                "--scenario",
                str(fake.scenario_path),
                *real[1:],
            ]

    def factory(program: Any, config_snapshot: Any, marker_store: Any) -> Any:
        fake.factory_calls.append((program, config_snapshot, marker_store))
        return FakeProgramAdapter(program, config_snapshot, marker_store)

    return factory


def make_service(
    project_root: Path,
    *,
    fake: "FakeClangd | None" = None,
    executable: Path | None = None,
    program_resolver: Callable[..., Any] | None = None,
    testcase: unittest.TestCase | None = None,
    **context_kwargs: Any,
) -> Any:
    """Construct the real service with the real (or fake-argv) adapter."""

    service_module = analysis_module("services.code_analysis")
    adapter_module = analysis_module("adapters.clangd")
    if program_resolver is None and fake is not None:
        program_resolver = fixed_resolver(FAKE_CLANGD)
    elif program_resolver is None and executable is not None:
        program_resolver = fixed_resolver(executable, version=None)
    kwargs: dict[str, Any] = {
        "adapter_factory": fake_adapter_factory(fake)
        if fake is not None
        else adapter_module.ClangdAdapter
    }
    if program_resolver is not None:
        kwargs["program_resolver"] = program_resolver
    service = service_module.CodeAnalysisService(
        make_context(project_root, **context_kwargs), **kwargs
    )
    if testcase is not None:
        testcase.addCleanup(service.shutdown)
    return service


# --------------------------------------------------------------- fake program
class FakeClangd:
    """A per-test fake clangd: live scenario file, JSONL log and launch argv."""

    def __init__(self, directory: Path, scenario: Mapping[str, Any] | None = None) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.scenario_path = self.directory / "scenario.json"
        self.log_path = self.directory / "fake-clangd.jsonl"
        self.log_path.touch()
        self.scenario: dict[str, Any] = {"log": str(self.log_path)}
        self.factory_calls: list[tuple[Any, ...]] = []
        self.configure(**dict(scenario or {}))

    @classmethod
    def attach(cls, directory: Path) -> "FakeClangd":
        """Reuse a fake prepared by another process without rewriting its scenario."""

        fake = cls.__new__(cls)
        fake.directory = Path(directory)
        fake.scenario_path = fake.directory / "scenario.json"
        fake.log_path = fake.directory / "fake-clangd.jsonl"
        fake.scenario = json.loads(fake.scenario_path.read_text(encoding="utf-8"))
        fake.factory_calls = []
        return fake

    def configure(self, **updates: Any) -> None:
        """Update the scenario read by the *next* fake process start."""

        self.scenario.update(updates)
        self.scenario["log"] = str(self.log_path)
        staged = self.scenario_path.with_suffix(".tmp")
        staged.write_text(json.dumps(self.scenario, indent=2), encoding="utf-8")
        for attempt in range(100):
            try:
                os.replace(staged, self.scenario_path)
                return
            except PermissionError:  # the peer is reading it right now (Windows)
                if attempt == 99:
                    raise
                time.sleep(0.01)

    def gate(self, name: str) -> Path:
        return self.directory / f"gate-{name}"

    def open_gate(self, name: str) -> None:
        self.gate(name).write_text("open", encoding="utf-8")

    def events(self, kind: str | None = None) -> list[dict[str, Any]]:
        records = [
            json.loads(line)
            for line in self.log_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        return [record for record in records if kind is None or record["event"] == kind]

    def received(self, method: str | None = None) -> list[dict[str, Any]]:
        return [
            record["message"]
            for record in self.events("received")
            if method is None or record["message"].get("method") == method
        ]

    def sent(self, method: str | None = None) -> list[dict[str, Any]]:
        return [
            record["message"]
            for record in self.events("sent")
            if method is None or record["message"].get("method") == method
        ]

    def client_responses(self) -> dict[Any, dict[str, Any]]:
        return {
            record["message"].get("id"): record["message"]
            for record in self.events("client_response")
        }

    def starts(self) -> list[dict[str, Any]]:
        return self.events("start")

    def pids(self) -> list[int]:
        pids = [record["pid"] for record in self.starts()]
        pids.extend(record["child_pid"] for record in self.events("grandchild"))
        return pids

    def wait_for(
        self, predicate: Callable[[], Any], *, timeout: float = 15.0, message: str = ""
    ) -> Any:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.02)
        raise AssertionError(f"timed out waiting for fake clangd: {message}\n{self.events()}")

    def wait_received(self, method: str, count: int = 1, timeout: float = 15.0) -> list[dict]:
        return self.wait_for(
            lambda: (found := self.received(method)) and len(found) >= count and found,
            timeout=timeout,
            message=f"{count} x {method}",
        )


def write_shim(directory: Path, name: str, scenario_path: Path) -> Path:
    """Write an executable launcher that runs the fake through this Python.

    Verified under real ``popen_owned`` on Windows (no ``shell=True``); argv is
    forwarded with ``%*``/``"$@"``.  Fixture argv must avoid cmd metacharacters.
    """

    python = Path(sys.executable)
    if os.name == "nt":
        shim = Path(directory) / f"{name}.cmd"
        shim.write_bytes(
            (f'@echo off\r\n"{python}" "{FAKE_CLANGD}" --scenario "{scenario_path}" %*\r\n').encode(
                "utf-8"
            )
        )
    else:
        shim = Path(directory) / name
        shim.write_text(
            f"#!/bin/sh\nexec '{python}' '{FAKE_CLANGD}' --scenario '{scenario_path}' \"$@\"\n",
            encoding="utf-8",
        )
        shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return shim


def pid_alive(pid: int) -> bool:
    import psutil

    try:
        process = psutil.Process(pid)
        return process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def wait_pids_gone(pids: Iterable[int], timeout: float) -> list[int]:
    deadline = time.monotonic() + timeout
    remaining = list(pids)
    while remaining and time.monotonic() < deadline:
        remaining = [pid for pid in remaining if pid_alive(pid)]
        if remaining:
            time.sleep(0.05)
    return remaining


def real_clangd_or_skip(testcase: unittest.TestCase) -> Path:
    configured = os.environ.get(REAL_CLANGD_ENV)
    if not configured:
        testcase.skipTest(
            f"UNMET PREREQUISITE (not a pass): set {REAL_CLANGD_ENV} to a real clangd "
            "executable supplied as a test-harness fixture"
        )
    executable = Path(configured)
    if not executable.is_file():
        raise AssertionError(f"{REAL_CLANGD_ENV} names a missing file: {executable}")
    return executable.resolve()


# ------------------------------------------------------------ project fixtures
class AnalysisProject:
    """A temporary project root with exact-byte sources and a database."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, relative: str) -> Path:
        return self.root / relative

    def write_bytes(self, relative: str, data: bytes) -> Path:
        target = self.path(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return target

    def write_text(self, relative: str, text: str) -> Path:
        """Write UTF-8 bytes exactly; ``\\r\\n`` in ``text`` is preserved."""

        return self.write_bytes(relative, text.encode("utf-8"))

    def write_config(self, document: Any) -> Path:
        raw = document if isinstance(document, str) else json.dumps(document)
        return self.write_text("byo-analysis.json", raw)

    def entry(
        self,
        source: str,
        *,
        directory: str | Path | None = None,
        arguments: list[str] | None = None,
        command: str | None = None,
        file_value: str | None = None,
        output: str | None = None,
    ) -> dict[str, Any]:
        record: dict[str, Any] = {
            "directory": str(directory if directory is not None else self.root),
            "file": file_value if file_value is not None else str(self.path(source)),
        }
        if arguments is None and command is None:
            arguments = ["clang", "-std=c11", "-c", str(self.path(source))]
        if arguments is not None:
            record["arguments"] = arguments
        if command is not None:
            record["command"] = command
        if output is not None:
            record["output"] = output
        return record

    def write_database(
        self,
        entries: list[Any] | str,
        relative: str = "compile_commands.json",
        *,
        escape_non_ascii: bool = False,
    ) -> Path:
        """Write a database as literal UTF-8 JSON by default.

        ``escape_non_ascii=True`` writes the equally valid ``\\uXXXX`` form
        (non-BMP characters become escaped UTF-16 surrogate pairs), which
        clangd 23.1.0 mishandles; tests opt into it explicitly.
        """

        if isinstance(entries, str):
            raw = entries
        else:
            raw = json.dumps(entries, indent=2, ensure_ascii=escape_non_ascii)
        return self.write_text(relative, raw)

    def tree_digest(self) -> dict[str, str]:
        """Every file/directory under the root, relative POSIX path -> sha256."""

        digest: dict[str, str] = {}
        for path in sorted(self.root.rglob("*")):
            relative = path.relative_to(self.root).as_posix()
            digest[relative] = sha256_file(path) if path.is_file() else "<dir>"
        return digest


# Contract "Local analysis writes" (ROOT clarification 8d18af22/cabf1a79): the
# service's own reports/metadata live under ``.firm/code-analysis/``, and stock
# clangd may keep its background index in
# ``<selected-database-directory>/.cache/clangd/index/`` as a generated
# ``.gitignore`` plus ``<basename>.<16 hex>.idx`` shards.  Nothing else may be
# created, removed or changed.  The earlier strict "only .firm" oracle was an
# overbroad expectation and stays recorded as a failed execution, not a pass.
SERVICE_WRITE_ROOT = ".firm/code-analysis"
_NATIVE_SHARD = re.compile(r"[^/]+\.[0-9A-Fa-f]{16}\.idx")


def permitted_analysis_write(relative: str, database_directory: str) -> bool:
    """Whether a project-relative POSIX path is a documented analysis write."""

    service = SERVICE_WRITE_ROOT.split("/")
    parts = relative.split("/")
    if parts[: len(service)] == service or parts == service[: len(parts)]:
        return True
    base = [part for part in database_directory.split("/") if part not in ("", ".")]
    cache = [*base, ".cache", "clangd", "index"]
    if parts[: len(base)] == base and len(base) < len(parts) <= len(cache):
        return parts == cache[: len(parts)]  # the cache directories themselves
    if len(parts) != len(cache) + 1 or parts[: len(cache)] != cache:
        return False
    return parts[-1] == ".gitignore" or _NATIVE_SHARD.fullmatch(parts[-1]) is not None


def unpermitted_tree_changes(
    before: Mapping[str, str], after: Mapping[str, str], database_directory: str
) -> list[str]:
    """Created/removed/changed paths outside the documented analysis writes.

    Protected inputs (sources, generated headers, database, ``.clangd``,
    config, board policy, any other ``.firm`` content) must be byte-identical.
    """

    changes = []
    for relative in sorted(set(before) | set(after)):
        old, new = before.get(relative), after.get(relative)
        if old == new or permitted_analysis_write(relative, database_directory):
            continue
        kind = "created" if old is None else "removed" if new is None else "changed"
        changes.append(f"{kind}: {relative}")
    return changes


@contextlib.contextmanager
def temporary_directory(prefix: str) -> Generator[Path, None, None]:
    directory = Path(tempfile.mkdtemp(prefix=prefix)).resolve()
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


C_SOURCE = (
    '#include "util.h"\n'
    "\n"
    "int helper(int value) { return value + 1; }\n"
    "\n"
    "int main(void) { return helper(add(1, 2)); }\n"
)
C_HEADER = "#pragma once\nstatic inline int add(int a, int b) { return a + b; }\n"


def simple_project(root: Path, *, sources: Mapping[str, str] | None = None) -> AnalysisProject:
    """Project with ``src/main.c`` (+ header) and a root database."""

    project = AnalysisProject(root)
    files = dict(sources or {"src/main.c": C_SOURCE, "src/util.h": C_HEADER})
    for relative, text in files.items():
        project.write_text(relative, text)
    units = [name for name in files if name.endswith((".c", ".cpp", ".cc"))]
    project.write_database(
        [
            project.entry(
                name,
                directory=project.root,
                arguments=[
                    "clang",
                    "-std=c11",
                    "-I",
                    str(project.path("src")),
                    "-c",
                    str(project.path(name)),
                ],
            )
            for name in units
        ]
    )
    return project


# --------------------------------------------------------- Unicode oracles
def encoded_units(text: str, encoding: str) -> int:
    if encoding == "utf-8":
        return len(text.encode("utf-8"))
    if encoding == "utf-16":
        return len(text.encode("utf-16-le")) // 2
    if encoding == "utf-32":
        return len(text)
    raise ValueError(encoding)


def lsp_character(line_text: str, column: int, encoding: str) -> int:
    """1-based code-point column -> 0-based LSP character in ``encoding``."""

    return encoded_units(line_text[: column - 1], encoding)


def lines_of(text: str) -> list[str]:
    """Split exactly at LSP line terminators (\\r\\n, \\n, \\r) without dropping text."""

    lines: list[str] = []
    current = []
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\r":
            lines.append("".join(current))
            current = []
            if index + 1 < len(text) and text[index + 1] == "\n":
                index += 1
        elif char == "\n":
            lines.append("".join(current))
            current = []
        else:
            current.append(char)
        index += 1
    lines.append("".join(current))
    return lines


def raw_range(start: tuple[int, int], end: tuple[int, int]) -> dict[str, Any]:
    return {
        "start": {"line": start[0], "character": start[1]},
        "end": {"line": end[0], "character": end[1]},
    }


def mapped_range(start: tuple[int, int], end: tuple[int, int]) -> dict[str, Any]:
    return {
        "start": {"line": start[0], "column": start[1]},
        "end": {"line": end[0], "column": end[1]},
    }


def location(uri: str, start: tuple[int, int], end: tuple[int, int]) -> dict[str, Any]:
    return {"uri": uri, "range": raw_range(start, end)}


# --------------------------------------------------------- envelope checks
def assert_envelope(
    testcase: unittest.TestCase,
    envelope: Any,
    operation: str,
    *,
    status: str | Iterable[str] | None = None,
) -> dict[str, Any]:
    if isinstance(envelope, str):
        envelope = json.loads(envelope)
    testcase.assertIsInstance(envelope, dict, envelope)
    missing = COMMON_FIELDS - set(envelope)
    testcase.assertFalse(missing, f"envelope lacks {sorted(missing)}: {envelope}")
    testcase.assertEqual(envelope["backend"], "clangd")
    testcase.assertEqual(envelope["operation"], operation)
    testcase.assertIn(envelope["status"], STATUS_VALUES)
    testcase.assertEqual(envelope["index_completeness"], "unverified")
    testcase.assertIsInstance(envelope["warnings"], list)
    testcase.assertTrue(all(isinstance(item, str) for item in envelope["warnings"]), envelope)
    if operation == "status":
        testcase.assertNotIn("source", envelope)
    if envelope["status"] in FAILURE_STATUSES:
        for name in ("code", "message", "remedy"):
            testcase.assertIn(name, envelope, f"failure envelope lacks {name}: {envelope}")
            testcase.assertIsInstance(envelope[name], str)
            testcase.assertTrue(envelope[name], f"empty {name}: {envelope}")
        testcase.assertIn(envelope["code"], ERROR_CODES, envelope)
    if envelope.get("compilation_database") is None:
        testcase.assertIsNone(envelope.get("compilation_database_sha256"), envelope)
    if status is not None:
        allowed = {status} if isinstance(status, str) else set(status)
        testcase.assertIn(envelope["status"], allowed, json.dumps(envelope, indent=2))
    json.dumps(envelope, allow_nan=False)  # strict JSON, no NaN/Infinity
    return envelope


def assert_failure(
    testcase: unittest.TestCase,
    envelope: Any,
    operation: str,
    status: str | Iterable[str],
    code: str | Iterable[str],
) -> dict[str, Any]:
    envelope = assert_envelope(testcase, envelope, operation, status=status)
    codes = {code} if isinstance(code, str) else set(code)
    testcase.assertIn(envelope.get("code"), codes, json.dumps(envelope, indent=2))
    return envelope


def assert_source(
    testcase: unittest.TestCase, envelope: Mapping[str, Any], path: Path
) -> dict[str, Any]:
    source = envelope.get("source")
    testcase.assertIsInstance(source, dict, envelope)
    testcase.assertTrue(same_path(source["path"], path), (source, path))
    testcase.assertEqual(source["sha256"], sha256_file(path))
    version = source["document_version"]
    testcase.assertIsInstance(version, int)
    testcase.assertNotIsInstance(version, bool)
    testcase.assertGreater(version, 0)
    return source


def assert_not_clean(testcase: unittest.TestCase, envelope: Mapping[str, Any]) -> None:
    """A stale/missing diagnostics answer must never look like a clean file."""

    testcase.assertNotEqual(envelope["status"], "ok", json.dumps(envelope, indent=2))
    testcase.assertNotEqual(envelope["status"], "not_found", envelope)


# --------------------------------------------------------- service threading
class Call:
    """Run a synchronous service call on a thread and keep its outcome."""

    def __init__(self, function: Callable[..., Any], **kwargs: Any) -> None:
        self.result: Any = None
        self.error: BaseException | None = None
        self.started = time.monotonic()
        self.finished: float | None = None
        self._thread = threading.Thread(target=self._run, args=(function, kwargs), daemon=True)
        self._thread.start()

    def _run(self, function: Callable[..., Any], kwargs: dict[str, Any]) -> None:
        try:
            self.result = function(**kwargs)
        except BaseException as exc:  # noqa: BLE001 - surfaced to the test
            self.error = exc
        finally:
            self.finished = time.monotonic()

    def join(self, timeout: float) -> Any:
        self._thread.join(timeout)
        if self._thread.is_alive():
            raise AssertionError(f"service call still blocked after {timeout:.1f}s")
        if self.error is not None:
            raise self.error
        return self.result

    @property
    def elapsed(self) -> float:
        assert self.finished is not None
        return self.finished - self.started


# --------------------------------------------------------- MCP server helpers
def mcp_blocks(server: Any, tool_name: str, arguments: Mapping[str, Any]) -> list[Any]:
    result = asyncio.run(server.mcp.call_tool(tool_name, dict(arguments)))
    if isinstance(result, tuple):
        result = result[0]
    return list(result)


def mcp_envelope(server: Any, tool_name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Call through RegistryFastMCP and parse the single analysis JSON block."""

    blocks = mcp_blocks(server, tool_name, arguments)
    text_blocks = [block for block in blocks if isinstance(getattr(block, "text", None), str)]
    if not text_blocks:
        raise AssertionError(f"{tool_name} returned no text block: {blocks!r}")
    try:
        payload = json.loads(text_blocks[0].text)
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"{tool_name} first block is not one intact JSON document: {text_blocks[0].text!r}"
        ) from exc
    if not isinstance(payload, dict):
        raise AssertionError(f"{tool_name} payload is not an object: {payload!r}")
    return payload


def mcp_refusal(server: Any, tool_name: str, arguments: Mapping[str, Any]) -> str | dict:
    """Return the SDK/schema refusal text, or the envelope if the call completed."""

    try:
        return mcp_envelope(server, tool_name, arguments)
    except Exception as exc:  # noqa: BLE001 - FastMCP surfaces refusals as errors
        if isinstance(exc, AssertionError):
            raise
        return str(exc)


def configure_server_program(
    server: Any,
    *,
    fake: FakeClangd | None = None,
    executable: Path | None = None,
    program_resolver: Callable[..., Any] | None = None,
) -> Any:
    """Install a harness program through the owner's ``BYO_MCP_TEST_SEAMS`` seam.

    ``configure_code_analysis_test_seams(program_resolver=, adapter_factory=)``
    returns the live service (also ``server._code_analysis_service``).
    """

    if program_resolver is None:
        if fake is not None:
            program_resolver = fixed_resolver(FAKE_CLANGD)
        elif executable is not None:
            program_resolver = fixed_resolver(executable, version=None)
    adapter_factory = fake_adapter_factory(fake) if fake is not None else None
    return server.configure_code_analysis_test_seams(
        program_resolver=program_resolver, adapter_factory=adapter_factory
    )


# --------------------------------------------------------- stdio server driver
# The bootstrap mirrors the installed ``pyocd-debug-mcp = server:main`` console
# entry (module imported once, then ``main()``), installs the harness program
# through the gated seam and never reaches a public tool argument.
_STDIO_BOOTSTRAP = """\
import json
import sys
from pathlib import Path

config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
sys.path.insert(0, config["server_root"])
from tests.code_analysis_support import FakeClangd, configure_server_program
import pyocd_debug_mcp.server as server

fake = FakeClangd.attach(Path(config["fake"])) if config.get("fake") else None
executable = Path(config["executable"]) if config.get("executable") else None
if fake is not None or executable is not None:
    configure_server_program(server, fake=fake, executable=executable)
sys.argv = [sys.argv[0]]
raise SystemExit(server.main())
"""


class AnalysisStdioServer:
    """The MCP server as a real stdio child with an isolated marker root."""

    def __init__(
        self,
        base: Path,
        project_root: Path,
        *,
        fake: FakeClangd | None = None,
        executable: Path | None = None,
    ) -> None:
        import subprocess

        self.base = Path(base)
        self.base.mkdir(parents=True, exist_ok=True)
        self.runs_root = self.base / "runs"
        self.marker_root = self.runs_root / "owned-processes"
        bootstrap = self.base / "analysis_stdio_bootstrap.py"
        bootstrap.write_text(_STDIO_BOOTSTRAP, encoding="utf-8")
        config = self.base / "analysis_stdio_bootstrap.json"
        config.write_text(
            json.dumps(
                {
                    "server_root": str(SERVER_ROOT),
                    "fake": str(fake.directory) if fake is not None else None,
                    "executable": str(executable) if executable is not None else None,
                }
            ),
            encoding="utf-8",
        )
        environment = os.environ.copy()
        environment.update(
            {
                "BYO_MCP_ARTIFACT_ROOT": str(project_root),
                "BYO_MCP_MONITOR_ROOT": str(self.base / "monitor"),
                "BYO_MCP_TEST_SEAMS": "1",
                "PYOCD_MCP_RUNS_ROOT": str(self.runs_root),
                "PYTHONUTF8": "1",
            }
        )
        environment.pop(REAL_CLANGD_ENV, None)
        self.proc = subprocess.Popen(  # noqa: S603 - fixed argv, test-owned files
            [sys.executable, str(bootstrap), str(config)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(project_root),
            env=environment,
        )
        self.stdout_lines: list[bytes] = []
        self.stderr = bytearray()
        self._next_id = 1
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_thread.start()

    def _drain_stderr(self) -> None:
        assert self.proc.stderr is not None
        for chunk in iter(lambda: self.proc.stderr.read(4096), b""):
            self.stderr.extend(chunk)

    def send(self, payload: Mapping[str, Any]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(payload).encode("utf-8") + b"\n")
        self.proc.stdin.flush()

    def read(self) -> dict[str, Any]:
        assert self.proc.stdout is not None
        while True:
            line = self.proc.stdout.readline()
            if not line:
                # Let the child finish and the drain thread collect its stderr
                # so the failure carries the actual cause.
                with contextlib.suppress(Exception):
                    self.proc.wait(timeout=10)
                self._stderr_thread.join(5)
                raise AssertionError(
                    f"server closed stdout unexpectedly (exit {self.proc.poll()}); stderr tail:\n"
                    + bytes(self.stderr[-4000:]).decode("utf-8", "replace")
                )
            self.stdout_lines.append(line)
            if line.strip():
                return json.loads(line)

    def request(self, method: str, params: Mapping[str, Any]) -> int:
        self._next_id += 1
        self.send({"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": dict(params)})
        return self._next_id

    def response(self, request_id: int) -> dict[str, Any]:
        while True:
            message = self.read()
            if message.get("id") == request_id and "method" not in message:
                return message

    def initialize(self) -> dict[str, Any]:
        self.send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "code-analysis-test", "version": "0"},
                },
            }
        )
        reply = self.response(1)
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        return reply

    def call(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        return self.response(
            self.request("tools/call", {"name": name, "arguments": dict(arguments)})
        )

    def call_envelope(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        reply = self.call(name, arguments)
        content = reply.get("result", {}).get("content") or []
        if not content or not isinstance(content[0].get("text"), str):
            raise AssertionError(f"{name} returned no text block: {reply}")
        return json.loads(content[0]["text"])

    def descendants(self) -> list[int]:
        import psutil

        try:
            return [child.pid for child in psutil.Process(self.proc.pid).children(recursive=True)]
        except psutil.NoSuchProcess:
            return []

    def markers(self) -> list[Path]:
        if not self.marker_root.is_dir():
            return []
        return sorted(self.marker_root.glob("*.json"))

    def close_stdin_and_wait(self, timeout: float) -> tuple[int, float]:
        assert self.proc.stdin is not None
        started = time.monotonic()
        self.proc.stdin.close()
        code = self.proc.wait(timeout=timeout)
        elapsed = time.monotonic() - started
        self._stderr_thread.join(5)
        assert self.proc.stdout is not None
        self.stdout_lines.extend(self.proc.stdout.readlines())
        self.proc.stdout.close()
        return code, elapsed

    def kill(self) -> None:
        import subprocess

        if self.proc.poll() is None:
            if os.name == "nt":
                subprocess.run(  # noqa: S603 - fixed argv
                    ["taskkill", "/PID", str(self.proc.pid), "/T", "/F"],
                    capture_output=True,
                    check=False,
                )
            else:
                self.proc.kill()
            self.proc.wait(timeout=10)
        for stream in (self.proc.stdin, self.proc.stdout):
            if stream is not None and not stream.closed:
                with contextlib.suppress(OSError):
                    stream.close()

    def stderr_text(self) -> str:
        return bytes(self.stderr).decode("utf-8", "replace")


def isolated_path_environment(directory: Path) -> dict[str, str]:
    """PATH containing only ``directory`` (plus Windows system dir for cmd)."""

    entries = [str(directory)]
    if os.name == "nt":
        system_root = os.environ.get("SystemRoot", r"C:\Windows")
        entries.append(str(Path(system_root) / "System32"))
    return {"PATH": os.pathsep.join(entries)}


# --------------------------------------------------------- shared test bases
def file_uri(path: Path) -> str:
    return Path(path).resolve().as_uri()


def query_params(fake: FakeClangd, method: str) -> list[dict[str, Any]]:
    return [message.get("params") or {} for message in fake.received(method)]


def first_location(testcase: unittest.TestCase, envelope: Mapping[str, Any]) -> dict[str, Any]:
    locations = envelope["data"]["locations"]
    testcase.assertTrue(locations, json.dumps(envelope, indent=2))
    return locations[0]


class FakeServiceCase(unittest.TestCase):
    """A real ``CodeAnalysisService`` + real ``ClangdAdapter`` against the fake peer."""

    sources: Mapping[str, str] = {"src/main.c": C_SOURCE, "src/util.h": C_HEADER}

    def scenario(self) -> dict[str, Any]:
        return {
            "responses": {
                "textDocument/definition": {
                    "result": {
                        "uri": "$request_uri",
                        "range": raw_range((2, 4), (2, 10)),
                    }
                }
            }
        }

    def setUp(self) -> None:
        directory = temporary_directory("byo-ca-")
        self.base = directory.__enter__()
        self.addCleanup(directory.__exit__, None, None, None)
        self.project = simple_project(self.base / "project", sources=self.sources)
        self.fake = FakeClangd(self.base / "fake", self.scenario())
        self.addCleanup(self._assert_fake_processes_released)
        self.service = make_service(self.project.root, fake=self.fake, testcase=self)

    def _assert_fake_processes_released(self) -> None:
        # Runs after the service shutdown cleanup (LIFO): no owned peer or its
        # grandchildren may outlive the service.
        remaining = wait_pids_gone(
            self.fake.pids(), ARGUMENT_TIMEOUT_GRACE_SECONDS + OWNED_CLEANUP_SECONDS
        )
        self.assertEqual(remaining, [], "fake clangd processes outlived service shutdown")

    def source(self, relative: str = "src/main.c") -> Path:
        return self.project.path(relative)

    def position_of(self, needle: str, relative: str = "src/main.c", occurrence: int = 0):
        """1-based (line, column) in code points of ``needle`` in the saved text."""

        text = self.project.path(relative).read_bytes().decode("utf-8")
        for number, line in enumerate(lines_of(text), start=1):
            start = 0
            while (index := line.find(needle, start)) >= 0:
                if occurrence == 0:
                    return number, index + 1
                occurrence -= 1
                start = index + 1
        raise AssertionError(f"{needle!r} not found in {relative}")

    def definition(self, relative: str = "src/main.c", needle: str = "helper", **kwargs: Any):
        line, column = self.position_of(needle, relative, kwargs.pop("occurrence", 0))
        arguments = {
            "file_path": str(self.source(relative)),
            "line": line,
            "column": column,
            "timeout_seconds": 10.0,
        }
        arguments.update(kwargs)
        return self.service.definition(**arguments)


# --------------------------------------------------------- fixture projects
def copy_fixture(name: str, destination: Path) -> AnalysisProject:
    source = FIXTURE_ROOT / name
    if not source.is_dir():
        raise AssertionError(f"missing fixture {source}")
    shutil.copytree(source, destination)
    return AnalysisProject(destination)


def host_database(project: AnalysisProject, units: Iterable[str], *, std: str = "c11") -> Path:
    """Write an absolute-path host database for real clangd (no compiler needed)."""

    entries = []
    for unit in units:
        language_std = "c++17" if unit.endswith((".cpp", ".cc")) else std
        entries.append(
            project.entry(
                unit,
                directory=project.root,
                arguments=[
                    "clang++" if unit.endswith((".cpp", ".cc")) else "clang",
                    f"-std={language_std}",
                    "-I",
                    str(project.path("include")),
                    "-c",
                    str(project.path(unit)),
                ],
            )
        )
    return project.write_database(entries)


ARM_GNU_ROOT_ENV = "BYO_MCP_TEST_ARM_GNU_ROOT"


def arm_gnu_root_or_skip(testcase: unittest.TestCase) -> Path:
    configured = os.environ.get(ARM_GNU_ROOT_ENV)
    if not configured:
        testcase.skipTest(
            f"UNMET PREREQUISITE (not a pass): set {ARM_GNU_ROOT_ENV} to an Arm GNU "
            "toolchain root supplied as a literal query-driver test fixture"
        )
    root = Path(configured).resolve()
    if not root.is_dir():
        raise AssertionError(f"{ARM_GNU_ROOT_ENV} names a missing directory: {root}")
    return root


def materialize_arm_fixture(
    destination: Path, arm_gnu_root: Path, *, query_drivers: bool = True
) -> AnalysisProject:
    """Copy the frozen ARM sources and materialize its exported database.

    The template keeps the exact executed argv (order, ``-I`` system include
    form, output) captured by ROOT; only machine-local prefixes are replaced.
    """

    project = copy_fixture("arm_firmware", destination)
    suffix = ".exe" if os.name == "nt" else ""

    def substitute(value: Any) -> Any:
        if isinstance(value, list):
            return [substitute(item) for item in value]
        if isinstance(value, dict):
            return {key: substitute(item) for key, item in value.items()}
        if not isinstance(value, str):
            return value
        for token, root in (("@PROJECT", project.root), ("@ARM_GNU", arm_gnu_root)):
            if f"{token}_NATIVE@" in value:
                tail = value.split(f"{token}_NATIVE@", 1)[1]
                value = str(root) + tail.replace("\\", os.sep)
            value = value.replace(f"{token}@", root.as_posix())
        if os.name != "nt":
            value = value.replace(".exe", suffix)
        return value

    template = project.path("build/compile_commands.template.json")
    database = substitute(json.loads(template.read_text(encoding="utf-8")))
    template.unlink()
    project.write_database(database, "build/compile_commands.json")
    config_template = project.path("byo-analysis.template.json")
    config = substitute(json.loads(config_template.read_text(encoding="utf-8")))
    config_template.unlink()
    if not query_drivers:
        config["clangd"]["query_driver_paths"] = []
    project.write_config(config)
    return project
