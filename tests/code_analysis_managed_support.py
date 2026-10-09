"""Independent Windows runtime fixtures, owned spawn observations and evidence.

These are generated development test bundles, never installed release evidence.
The fixture writes the existing ReleaseManifest shape and CycloneDX ownership;
it does not validate them or duplicate the resolver's decisions. Protocol tests
redirect only an already selected executable's spawn to the existing fake peer.
"""

from __future__ import annotations

import contextlib
import ctypes
import hashlib
import importlib
import json
import os
import sys
import threading
import time
import traceback
import unittest
import uuid
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / ".agent-workspace" / "managed-runtime-evidence"
NATIVE_INPUT_ENV = "BYO_MANAGED_TEST_INPUT_RECORD"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def production(name: str) -> Any:
    module = importlib.import_module("pyocd_debug_mcp." + name)
    assert module.__file__ is not None
    assert Path(module.__file__).resolve().is_relative_to(ROOT / "src"), module.__file__
    assert_import_roots()
    return module


def assert_import_roots() -> None:
    for name, module in tuple(sys.modules.items()):
        module_path = getattr(module, "__file__", None)
        if name.startswith("pyocd_debug_mcp") and module_path:
            assert Path(module_path).resolve().is_relative_to(ROOT / "src"), (
                name,
                module.__file__,
            )


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def tree_hashes(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): digest(p) for p in root.rglob("*") if p.is_file()}


def default_mapping() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "platform": "windows",
        "architecture": "x86_64",
        "programs": {
            "cppcheck": {
                "version": "2.22.0",
                "executable": "analysis/cppcheck/cppcheck.exe",
                "cfg_dir": "analysis/cppcheck/cfg",
                "platforms_dir": "analysis/cppcheck/platforms",
                "licenses": [
                    "analysis/cppcheck/licenses/" + n
                    for n in (
                        "COPYING",
                        "simplecpp-LICENSE",
                        "tinyxml2-LICENSE",
                        "picojson-LICENSE",
                    )
                ],
                "dependencies": [],
                "sbom_ref": "byo-analysis:cppcheck",
            },
            "clangd": {
                "version": "23.1.0",
                "executable": "analysis/clangd/bin/clangd.exe",
                "resource_dir": "analysis/clangd/lib/clang/23",
                "licenses": ["analysis/clangd/LICENSE.TXT"],
                "dependencies": [],
                "sbom_ref": "byo-analysis:clangd",
            },
        },
    }


class RuntimeFixture:
    """Small inventoried synthetic payload, or exact copied native vendor set.

    File components use the existing builder's byo-native:<path> convention.
    The accepted contract requires app->file dependencies and license ownership,
    without prescribing another file-reference vocabulary.
    """

    def __init__(self, root: Path, native: dict[str, Any] | None = None) -> None:
        self.root = root.resolve()
        self.mapping = default_mapping()
        self.mapping_path = self.root / "analysis/runtime.json"
        self.manifest_path = self.root / "release-manifest.json"
        self.sbom_path = self.root / "sbom.cdx.json"
        self.root.mkdir(parents=True, exist_ok=True)
        self.native = native
        if native is None:
            # A real AMD64 PE header, deliberately not an analyzer implementation.
            # Only redirected protocol/version tests execute this synthetic case.
            for program in self.mapping["programs"].values():
                self.put(program["executable"], Path(sys.executable).read_bytes())
                for license_path in program["licenses"]:
                    self.put(license_path, b"Synthetic unit-test notice; not vendor provenance.\n")
            self.put("analysis/cppcheck/cfg/std.cfg", b'<def format="2"/>\n')
            self.put("analysis/cppcheck/platforms/win64.xml", b"<platform/>\n")
            self.put(
                "analysis/clangd/lib/clang/23/include/stddef.h", b"typedef __SIZE_TYPE__ size_t;\n"
            )
            self.put("analysis/clangd/lib/clang/23/include/stdint.h", b"typedef int int32_t;\n")
        else:
            for entry in native["payload"]:
                source = Path(entry["source"])
                assert digest(source) == entry["sha256"], source
                assert source.stat().st_size == entry["size"], source
                self.put(entry["runtime_path"], source.read_bytes())
        self.seal()

    def put(self, relative: str, data: bytes) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    @staticmethod
    def kind(path: str) -> tuple[str, bool]:
        if path.endswith(".exe"):
            return "analysis-executable", True
        if path.endswith(".dll"):
            return "analysis-dependency", False
        if "/licenses/" in path or path.endswith("LICENSE.TXT"):
            return "license", False
        if path.startswith("analysis/clangd/lib/"):
            return "analysis-resource", False
        return "analysis-data", False

    def seal(self, raw_mapping: bytes | None = None) -> None:
        """Generate fixture metadata before resolution, never repair in a test call."""
        if raw_mapping is None:
            write_json(self.mapping_path, self.mapping)
        else:
            self.mapping_path.write_bytes(raw_mapping)
        components: list[dict[str, Any]] = []
        dependencies: list[dict[str, Any]] = []
        for name, program in self.mapping["programs"].items():
            paths = sorted(
                p.relative_to(self.root).as_posix()
                for p in (self.root / "analysis" / name).rglob("*")
                if p.is_file()
            )
            components.append(
                {
                    "type": "application",
                    "bom-ref": program["sbom_ref"],
                    "name": name,
                    "version": program["version"],
                    "licenses": [{"license": {"name": p}} for p in program["licenses"]],
                    "properties": [
                        {
                            "name": "byo:analysis:upstream",
                            "value": self.native["provenance"][name]["upstream"]
                            if self.native
                            else "synthetic unit test; no upstream artifact",
                        },
                        {
                            "name": "byo:analysis:recipe",
                            "value": self.native["provenance"][name]["recipe"]
                            if self.native
                            else "synthetic bytes; redirected protocol payload",
                        },
                    ],
                }
            )
            refs = []
            for path in paths:
                reference = "byo-native:" + path
                refs.append(reference)
                components.append(
                    {
                        "type": "file",
                        "bom-ref": reference,
                        "name": path,
                        "hashes": [{"alg": "SHA-256", "content": digest(self.root / path)}],
                        "properties": [
                            {
                                "name": "byo:analysis:upstream-path",
                                "value": next(
                                    (
                                        e["upstream_path"]
                                        for e in self.native["payload"]
                                        if e["runtime_path"] == path
                                    ),
                                    path,
                                )
                                if self.native
                                else path,
                            }
                        ],
                    }
                )
            dependencies.append({"ref": program["sbom_ref"], "dependsOn": refs})
        self.sbom = {
            "bomFormat": "CycloneDX",
            "specVersion": "1.6",
            "version": 1,
            "metadata": {
                "component": {
                    "type": "application",
                    "name": "BYO",
                    "version": "0.1.0",
                    "bom-ref": "pkg:generic/byo@0.1.0",
                }
            },
            "components": components,
            "dependencies": dependencies,
        }
        write_json(self.sbom_path, self.sbom)
        self.inventory = []
        for p in sorted(self.root.rglob("*")):
            if not p.is_file() or p == self.manifest_path:
                continue
            relative = p.relative_to(self.root).as_posix()
            kind, executable = (
                ("metadata", False)
                if p == self.mapping_path
                else (("sbom", False) if p == self.sbom_path else self.kind(relative))
            )
            self.inventory.append(
                {
                    "path": relative,
                    "sha256": digest(p),
                    "size": p.stat().st_size,
                    "kind": kind,
                    "executable": executable,
                }
            )
        revision = {"repository": "synthetic-test-input", "commit": "0" * 40}
        self.manifest = {
            "schema": 1,
            "product": "byo",
            "version": "0.1.0",
            "channel": "development",
            "platform": "windows",
            "architecture": "x86_64",
            "launcher_protocol": 1,
            "sidecar_protocol": 1,
            "worker_protocol": 1,
            "workflow_protocol": 1,
            "capsule_schema": 1,
            "project_state_schema": 1,
            "development_unsigned": True,
            "source": {n: dict(revision) for n in ("installer", "agent_workspace", "firmware_mcp")},
            "toolchain": {n: "test fixture" for n in ("rustc", "cargo", "python", "nuitka")},
            "files": self.inventory,
        }
        write_json(self.manifest_path, self.manifest)

    def inventory_entry(self, path: str) -> dict[str, Any]:
        return next(f for f in self.inventory if f["path"] == path)

    def save_manifest(self) -> None:
        write_json(self.manifest_path, self.manifest)

    def save_sbom(self) -> None:
        write_json(self.sbom_path, self.sbom)
        self.inventory_entry("sbom.cdx.json").update(
            sha256=digest(self.sbom_path), size=self.sbom_path.stat().st_size
        )
        self.save_manifest()

    def replace_mapping(self, value: Any) -> None:
        if isinstance(value, bytes):
            self.mapping_path.write_bytes(value)
        else:
            write_json(self.mapping_path, value)
        self.inventory_entry("analysis/runtime.json").update(
            sha256=digest(self.mapping_path), size=self.mapping_path.stat().st_size
        )
        self.save_manifest()

    def context(self, project: Path, managed: bool = True) -> Any:
        return production("code_analysis_runtime").AnalysisRuntimeContext(
            project, self.root, managed
        )


def native_start_token(pid: int) -> str | None:
    """Independent GetProcessTimes observation; unknown access is an error."""
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        error = ctypes.get_last_error()
        if error == 87:
            return None
        raise OSError(error, "Cannot determine native process identity", str(pid))
    try:
        code = wintypes.DWORD()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
            raise ctypes.WinError(ctypes.get_last_error())
        # Popen retains a handle to a terminated Windows process object. Its
        # creation time remains readable; require STILL_ACTIVE for live identity.
        if code.value != 259:
            return None
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
            raise ctypes.WinError(ctypes.get_last_error())
        return f"win:{times[0].dwHighDateTime:08x}{times[0].dwLowDateTime:08x}"
    finally:
        kernel.CloseHandle(handle)


class OwnedSpawns:
    def __init__(self, directory: Path, redirect: Any = None) -> None:
        self.directory = directory
        self.redirect = redirect
        self.children: list[tuple[Any, Path | None, dict[str, Any]]] = []
        self.calls: list[list[str]] = []
        self.lock = threading.Lock()
        self.runtime = production("code_analysis_runtime")
        self.processes = production("kernel.processes")
        self.store = self.processes.ProcessMarkerStore(directory / "markers")
        self.original = self.runtime.popen_owned

    def __enter__(self) -> "OwnedSpawns":
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(
            mock.patch.object(self.runtime, "ProcessMarkerStore", return_value=self.store)
        )
        self.stack.enter_context(mock.patch.object(self.runtime, "popen_owned", self.spawn))
        return self

    def spawn(self, argv: list[str], **kwargs: Any) -> Any:
        requested = list(argv)
        actual = self.redirect(requested) if self.redirect else requested
        kwargs["marker_store"] = self.store
        process, marker = self.original(actual, **kwargs)
        token = native_start_token(process.pid)
        assert token is not None, "Spawn returned without an observable creation identity"
        record = {
            "requested_argv": requested,
            "actual_argv": actual,
            "pid": process.pid,
            "native_creation": token,
            "marker": json.loads(marker.read_bytes()) if marker else None,
            "cwd": str(kwargs.get("cwd")),
            "spawn_at": time.monotonic(),
        }
        with self.lock:
            self.calls.append(requested)
            self.children.append((process, marker, record))
        return process, marker

    def check_cleanup(self) -> None:
        for process, marker, record in self.children:
            record["exit_code"] = process.poll()
            record["identity_matches_after"] = (
                native_start_token(process.pid) == record["native_creation"]
            )
            record["marker_exists_after"] = marker.exists() if marker else None
            assert record["exit_code"] is not None, record
            assert record["identity_matches_after"] is False, record
            assert record["marker_exists_after"] is False, record
        assert not list(self.store.root.glob("*.json")), "Owned recovery marker remains"

    def __exit__(self, *exc: Any) -> None:
        # Preserve an unclean observation before emergency cleanup of only these children.
        self.directory.mkdir(parents=True, exist_ok=True)
        try:
            self.check_cleanup()
        finally:
            write_json(self.directory / "processes.json", [r for _, _, r in self.children])
            for process, marker, _ in self.children:
                if process.poll() is None and self.processes.terminate_process_group(process):
                    self.store.remove(marker)
            self.stack.close()


def protocol_redirect(fixture: RuntimeFixture, fake: Any) -> Any:
    def redirect(argv: list[str]) -> list[str]:
        selected = Path(argv[0]).resolve()
        names = {
            (fixture.root / p["executable"]).resolve(): name
            for name, p in fixture.mapping["programs"].items()
        }
        assert selected in names, ("Untrusted executable selected", argv)
        if names[selected] == "cppcheck":
            assert argv[1:] == ["--version"], argv
            return [sys.executable, "-c", "print('Cppcheck 2.22.0')"]
        from tests.code_analysis_support import FAKE_CLANGD

        return [sys.executable, str(FAKE_CLANGD), "--scenario", str(fake.scenario_path), *argv[1:]]

    return redirect


class ManagedCase(unittest.TestCase):
    def setUp(self) -> None:
        if os.name != "nt":
            self.skipTest("Windows x86_64 only; other platforms are outside this assignment")
        # Keep native CreateProcess cwd below MAX_PATH despite long lane roots.
        # Every case has a unique retained directory and explicit identity.
        self.base = EVIDENCE / "c" / uuid.uuid4().hex[:6]
        self.base.mkdir(parents=True)
        write_json(self.base / "case.json", {"case": self.id()})
        self.project = self.base / "P caf\u00e9 \u4e2d \U0001f600"
        self.project.mkdir()
        from tests.code_analysis_support import AnalysisProject, FakeClangd

        self.project_fixture = AnalysisProject(self.project)
        self.source = self.project_fixture.write_text(
            "main.c", "int value(void) { return 7; }\nint main(void) { return value(); }\n"
        )
        self.project_fixture.write_database([self.project_fixture.entry("main.c")])
        self.fixture = RuntimeFixture(self.base / "R \u4e2d")
        self.fake = FakeClangd(self.base / "peer")
        self.runtime = production("code_analysis_runtime")
        self.environment = mock.patch.dict(
            os.environ,
            {
                "PATH": str(self.base / "empty-path"),
                "PATHEXT": ".EXE",
                "PYOCD_MCP_RUNS_ROOT": str(self.base / "runs"),
                "BYO_MCP_MONITOR_ROOT": str(self.base / "monitor"),
            },
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.context = self.fixture.context(self.project)

    def resolve(self, name: str = "clangd", budget: float = 30, context: Any = None) -> Any:
        return self.runtime.resolve_analysis_program(
            context or self.context, name, time.monotonic() + budget
        )

    def assert_usable(self, program: Any, name: str = "clangd") -> None:
        self.assertIsNotNone(program.executable, program)
        self.assertFalse(program.problems, program)
        self.assertEqual(program.origin, "managed_runtime", program)
        self.assertEqual(
            Path(program.executable).resolve(),
            (self.fixture.root / self.fixture.mapping["programs"][name]["executable"]).resolve(),
        )

    def observer(self) -> OwnedSpawns:
        return OwnedSpawns(self.base, protocol_redirect(self.fixture, self.fake))

    def require_valid_control(self, observer: OwnedSpawns, name: str = "clangd") -> None:
        control = RuntimeFixture(self.base / ("v" + uuid.uuid4().hex[:6]))
        before = len(observer.calls)
        # This different root has no receipt; the invalid fixture also remains
        # unseen, so its syntax/payload failure cannot be masked by invalidation.
        with mock.patch.object(observer, "redirect", protocol_redirect(control, self.fake)):
            result = self.resolve(name, context=control.context(self.project))
        self.assertIsNotNone(result.executable, result)
        self.assertFalse(result.problems, result)
        self.assertEqual(result.origin, "managed_runtime", result)
        self.assertEqual(
            Path(result.executable).resolve(),
            (control.root / control.mapping["programs"][name]["executable"]).resolve(),
        )
        self.assertEqual(len(observer.calls), before + 1, "Valid control must probe exactly once")

    def assert_rejected_without_spawn(
        self, observer: OwnedSpawns, hint: str, name: str = "clangd"
    ) -> Any:
        before = len(observer.calls)
        result = self.resolve(name)
        self.assertTrue(result.problems, result)
        self.assertEqual(result.problems[0].code, "analysis/executable-unavailable", result)
        description = " ".join(
            str(getattr(p, k, "")) for p in result.problems for k in ("message", "field", "remedy")
        )
        self.assertIn(hint.lower(), description.lower(), description)
        self.assertTrue(all(p.remedy for p in result.problems), result)
        self.assertEqual(len(observer.calls), before, "Executable spawned before failed integrity")
        self.assertNotEqual(result.origin, "path", result)
        write_json(
            self.base / ("rejection-" + str(before) + ".json"),
            {
                "hint": hint,
                "description": description,
                "program": repr(result),
            },
        )
        return result

    def service(self, observer: OwnedSpawns) -> Any:
        adapter = production("adapters.clangd")
        service = production("services.code_analysis").CodeAnalysisService(
            self.context,
            adapter_factory=lambda p, s, _: adapter.ClangdAdapter(p, s, observer.store),
        )
        self.addCleanup(service.shutdown)
        return service


def run_named_suite(modules: list[str], output: Path) -> int:
    """unittest result capture, including every subcase and required skip."""
    records: list[dict[str, Any]] = []

    class Results(unittest.TextTestResult):
        def __init__(self, stream: Any, descriptions: bool, verbosity: int) -> None:
            super().__init__(stream, descriptions, verbosity)

        def startTest(self, test: Any) -> None:
            self.started = time.monotonic()
            super().startTest(test)

        def addSuccess(self, test: Any) -> None:
            records.append(
                {"case": test.id(), "outcome": "PASS", "elapsed": time.monotonic() - self.started}
            )
            super().addSuccess(test)

        def addFailure(self, test: Any, err: Any) -> None:
            records.append(
                {
                    "case": test.id(),
                    "outcome": "FAIL",
                    "detail": "".join(traceback.format_exception(*err)),
                }
            )
            super().addFailure(test, err)

        def addError(self, test: Any, err: Any) -> None:
            records.append(
                {
                    "case": test.id(),
                    "outcome": "ERROR",
                    "detail": "".join(traceback.format_exception(*err)),
                }
            )
            super().addError(test, err)

        def addSkip(self, test: Any, reason: str) -> None:
            records.append({"case": test.id(), "outcome": "SKIP", "reason": reason})
            super().addSkip(test, reason)

        def addSubTest(self, test: Any, subtest: Any, err: Any) -> None:
            records.append(
                {
                    "case": subtest.id(),
                    "outcome": "PASS" if err is None else "FAIL",
                    "detail": None if err is None else "".join(traceback.format_exception(*err)),
                }
            )
            super().addSubTest(test, subtest, err)

    suite = unittest.TestLoader().loadTestsFromNames(modules)
    result = unittest.TextTestRunner(verbosity=2, resultclass=Results).run(suite)
    assert_import_roots()
    write_json(
        output,
        {
            "scope": "PREPARATION ONLY",
            "argv": sys.argv,
            "cases": records,
            "tests_run": result.testsRun,
            "failures": len(result.failures),
            "errors": len(result.errors),
            "skips": len(result.skipped),
            "imports": {
                n: m.__file__
                for n, m in sys.modules.items()
                if n.startswith("pyocd_debug_mcp") and getattr(m, "__file__", None)
            },
        },
    )
    return 0 if result.wasSuccessful() and not result.skipped else 1


if __name__ == "__main__":
    raise SystemExit(run_named_suite(sys.argv[2:], Path(sys.argv[1])))
