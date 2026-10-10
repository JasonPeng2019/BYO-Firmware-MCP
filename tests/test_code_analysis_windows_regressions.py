"""Installed-like Windows query-driver and real clangd replacement regressions."""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import threading
import time
import unittest
from ctypes import wintypes
from pathlib import Path
from typing import Any
from unittest.mock import patch

from pyocd_debug_mcp.adapters.clangd import ClangdAdapter, ClangdFailure
from pyocd_debug_mcp.code_analysis_config import load_analysis_config
from pyocd_debug_mcp.kernel import processes
from pyocd_debug_mcp.services.code_analysis import CodeAnalysisService
from tests.code_analysis_support import (
    AnalysisProject,
    AnalysisStdioServer,
    FakeClangd,
    arm_gnu_root_or_skip,
    fixed_resolver,
    fake_adapter_factory,
    make_context,
    real_clangd_or_skip,
    sha256_file,
    simple_project,
    temporary_directory,
)


def record(name: str, data: dict[str, Any]) -> None:
    if directory := os.environ.get("BYO_WINDOWS_REGRESSION_EVIDENCE"):
        target = Path(directory)
        target.mkdir(parents=True, exist_ok=True)
        (target / (name + ".json")).write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )


@unittest.skipUnless(os.name == "nt", "Windows extended paths and Job lifecycle")
class WindowsQueryDriver(unittest.TestCase):
    def test_relative_compiler_under_extended_project_root_is_exact(self) -> None:
        with temporary_directory("byo-driver-") as base:
            project = AnalysisProject(base / "installed project \u00b5 \u6d4b\u8bd5")
            compiler = project.path("tools/compiler.exe")
            compiler.parent.mkdir()
            shutil.copyfile(
                arm_gnu_root_or_skip(self) / "bin/arm-none-eabi-g++.exe",
                compiler,
            )
            project.write_text("src/main.cpp", "int value;\n")
            project.write_database([project.entry("src/main.cpp")])
            project.write_config(
                {"schema_version": 1, "clangd": {"query_driver_paths": ["tools/compiler.exe"]}}
            )
            extended = Path("\\\\?\\" + str(project.root))
            ordinary = load_analysis_config(project.root)
            installed = load_analysis_config(extended)
            data = {
                "ordinary_root": str(project.root),
                "extended_root": str(extended),
                "compiler_sha256": sha256_file(compiler),
                "ordinary_drivers": list(map(str, ordinary.query_driver_paths)),
                "installed_drivers": list(map(str, installed.query_driver_paths)),
                "problems": [p.to_payload() for p in installed.problems],
                "same_executable": bool(installed.query_driver_paths)
                and installed.query_driver_paths[0].samefile(compiler),
                "source_path_utf16_units": len(
                    str(project.path("src/main.cpp")).encode("utf-16-le")
                )
                // 2,
            }
            record("query-driver", data)
            self.assertEqual(ordinary.problems, [], data)
            self.assertEqual(installed.problems, [], data)
            self.assertEqual(len(installed.query_driver_paths), 1)
            self.assertTrue(installed.query_driver_paths[0].samefile(compiler))
            # The string passed to clangd's glob parser must also be literal.
            self.assertFalse(any(c in str(installed.query_driver_paths[0]) for c in "*?[],{}"))

    def test_extended_root_does_not_allow_raw_patterns_or_network_inputs(self) -> None:
        with temporary_directory("byo-driver-refusals-") as base:
            project = AnalysisProject(base / "project")
            project.write_text("src/main.cpp", "int value;\n")
            project.write_database([project.entry("src/main.cpp")])
            project.write_text("tools/compiler.cmd", "@echo off\n")
            project.write_text("tools/compiler.txt", "not executable\n")
            project.write_text("tools/fake.exe", "not an executable image\n")
            shutil.copyfile(
                arm_gnu_root_or_skip(self) / "bin/arm-none-eabi-g++.exe",
                project.path("tools/compiler.exe"),
            )
            extended = Path("\\\\?\\" + str(project.root))
            observations = []
            for value in (
                None,
                7,
                "",
                "tools/\x00compiler.exe",
                "tools/*.exe",
                "tools/compile?.exe",
                "tools/[compiler].exe",
                "tools/compiler.exe,tools/other.exe",
                "tools/{compiler}.exe",
                "tools/*/../compiler.exe",
                "tools/missing.exe",
                "tools/compiler.cmd",
                "tools/compiler.txt",
                "tools/fake.exe",
                "tools",
                "https://example.invalid/compiler.exe",
                "file:///C:/compiler.exe",
                "\\\\server\\share\\compiler.exe",
                "//server/share/compiler.exe",
                "\\\\?\\UNC\\server\\share\\compiler.exe",
                "\\\\?\\" + str(project.path("tools/compiler.exe")),
            ):
                with self.subTest(value=value):
                    project.write_config(
                        {"schema_version": 1, "clangd": {"query_driver_paths": [value]}}
                    )
                    snapshot = load_analysis_config(extended)
                    observations.append(
                        {
                            "input": value,
                            "problems": [problem.to_payload() for problem in snapshot.problems],
                            "drivers": list(map(str, snapshot.query_driver_paths)),
                        }
                    )
                    self.assertTrue(snapshot.problems)
                    self.assertEqual(snapshot.query_driver_paths, ())
            record("query-driver-refusals", {"cases": observations})


class WindowsIdentity:
    """Hold a native process handle so PID reuse cannot hide a surviving child."""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        self.api.OpenProcess.restype = wintypes.HANDLE
        self.api.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (
            ctypes.POINTER(wintypes.FILETIME),
        ) * 4
        self.api.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        self.api.CloseHandle.argtypes = (wintypes.HANDLE,)
        self.api.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
        self.handle = self.api.OpenProcess(0x100000 | 0x1000 | 1, False, pid)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        creation, exit_time, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not self.api.GetProcessTimes(
            self.handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        self.creation = (creation.dwHighDateTime << 32) | creation.dwLowDateTime

    def evidence(self) -> dict[str, Any]:
        wait = self.api.WaitForSingleObject(self.handle, 0)
        if wait not in (0, 258):
            raise ctypes.WinError(ctypes.get_last_error())
        return {"pid": self.pid, "creation_filetime": self.creation, "alive": wait == 258}

    def close(self) -> bool:
        # Emergency test cleanup occurs only after the post-EOF observation.
        emergency = self.evidence()["alive"]
        if emergency:
            if not self.api.TerminateProcess(self.handle, 99):
                raise ctypes.WinError(ctypes.get_last_error())
            if self.api.WaitForSingleObject(self.handle, 5000) != 0:
                raise AssertionError("test emergency cleanup unconfirmed")
        self.api.CloseHandle(self.handle)
        return emergency


@unittest.skipUnless(os.name == "nt", "Windows real clangd replacement and EOF")
class WindowsClangdReplacement(unittest.TestCase):
    def test_responsive_replacement_exchanges_shutdown_and_releases_stdin_before_job_cleanup(
        self,
    ) -> None:
        executable = real_clangd_or_skip(self)
        compiler = arm_gnu_root_or_skip(self) / "bin/arm-none-eabi-g++.exe"
        with temporary_directory("byo-retirement-") as base:
            project = AnalysisProject(base / "project with spaces")
            adapters: list[ClangdAdapter] = []
            identities: list[WindowsIdentity] = []
            cleanup_observations: list[dict[str, Any]] = []
            entries = []

            def compile_unit(name: str) -> None:
                project.write_text(name, "int value;\n")
                argv = [str(compiler), "-c", str(project.path(name)), "-o", str(base / "unit.o")]
                result = subprocess.run(argv, cwd=project.root, capture_output=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                entries.append(project.entry(name, arguments=argv))
                project.write_database(entries)

            def factory(program: Any, snapshot: Any, marker_store: Any) -> ClangdAdapter:
                adapter = ClangdAdapter(
                    program, snapshot, processes.ProcessMarkerStore(base / "markers")
                )
                adapters.append(adapter)
                return adapter

            original_terminate = processes.terminate_process_group

            def observe_cleanup(process: subprocess.Popen[Any]) -> bool:
                observation = {
                    "pid": process.pid,
                    "stdin_closed": process.stdin is None or process.stdin.closed,
                    "leader_exit_before_cleanup": process.poll(),
                }
                started = time.monotonic()
                cleaned = original_terminate(process)
                observation.update(cleaned=cleaned, seconds=time.monotonic() - started)
                cleanup_observations.append(observation)
                return cleaned

            compile_unit("main.cpp")
            service = CodeAnalysisService(
                make_context(project.root), factory, program_resolver=fixed_resolver(executable)
            )
            data: dict[str, Any] = {"cleanup": cleanup_observations, "calls": []}
            try:
                with patch(
                    "pyocd_debug_mcp.adapters.clangd.terminate_process_group", observe_cleanup
                ):
                    first = service.diagnostics("main.cpp", timeout_seconds=60)
                    data["calls"].append(first)
                    self.assertEqual(first["status"], "ok", first)
                    assert adapters[0].process is not None
                    identities.append(WindowsIdentity(adapters[0].process.pid))
                    compile_unit("empty.cpp")
                    replacement = service.diagnostics("empty.cpp", timeout_seconds=60)
                    data["calls"].append(replacement)
                    self.assertEqual(replacement["status"], "ok", replacement)
                    self.assertEqual(len(adapters), 2)
                    assert adapters[1].process is not None
                    identities.append(WindowsIdentity(adapters[1].process.pid))
                    data["after_replacement"] = [identity.evidence() for identity in identities]
                    service.shutdown()
                data.update(
                    after_shutdown=[identity.evidence() for identity in identities],
                    stderr=[adapter.stderr_tail for adapter in adapters],
                    live_threads=[
                        thread.name
                        for adapter in adapters
                        for thread in adapter._threads
                        if thread.is_alive()
                    ],
                    pending_waiters=[len(adapter._pending) for adapter in adapters],
                    markers=list(map(str, (base / "markers").glob("*.json"))),
                )
                self.assertFalse(data["after_replacement"][0]["alive"], data)
                self.assertTrue(data["after_replacement"][1]["alive"], data)
                self.assertIn("<-- shutdown", adapters[0].stderr_tail, data)
                self.assertIn("<-- exit", adapters[0].stderr_tail, data)
                self.assertTrue(all(item["stdin_closed"] for item in cleanup_observations), data)
                self.assertFalse(any(item["alive"] for item in data["after_shutdown"]), data)
                self.assertEqual(data["live_threads"], [], data)
                self.assertEqual(data["pending_waiters"], [0, 0], data)
                self.assertEqual(data["markers"], [], data)
            finally:
                try:
                    service.shutdown()
                finally:
                    data["emergency_cleanup"] = [identity.close() for identity in identities]
                    record("retirement", data)

    def test_truthful_database_change_replaces_child_and_eof_clears_owned_identities(self) -> None:
        self.exercise_stdio_replacement("installed project", "")

    def test_unicode_paths_and_crlf_replacement_clean_eof(self) -> None:
        self.exercise_stdio_replacement(
            "installed project \u00b5 \u6d4b\u8bd5 \U0001f600",
            "/* \u00b5 \u6d4b\u8bd5 \U0001f600 */ ",
        )

    def exercise_stdio_replacement(self, root_name: str, prefix: str) -> None:
        executable = real_clangd_or_skip(self)
        compiler = arm_gnu_root_or_skip(self) / "bin/arm-none-eabi-g++.exe"
        with temporary_directory("byo-replacement-") as base:
            project = AnalysisProject(base / root_name)
            ending = "\r\n" if prefix else "\n"
            clean_text = prefix + "int main() { return 0; }" + ending
            project.write_text("src/main.cpp", clean_text)
            entries = []

            def add_entry(source: str) -> None:
                argv = [
                    str(compiler),
                    "-mcpu=cortex-m4",
                    "-mthumb",
                    "-std=c++17",
                    "-c",
                    str(project.path(source)),
                    "-o",
                    # The ARM assembler cannot open Unicode output paths. Keep
                    # its genuine output ASCII while testing Unicode inputs.
                    str(base / (Path(source).stem + ".o")),
                ]
                result = subprocess.run(argv, cwd=project.root, capture_output=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                entries.append(project.entry(source, arguments=argv))
                project.write_database(entries, "build/compile_commands.json")

            add_entry("src/main.cpp")
            server = AnalysisStdioServer(base / "server", project.root, executable=executable)
            identities: list[WindowsIdentity] = []
            data: dict[str, Any] = {
                "clangd": str(executable),
                "clangd_sha256": sha256_file(executable),
                "compiler": str(compiler),
                "compiler_sha256": sha256_file(compiler),
                "calls": [],
                "compile_commands": entries,
                "source_path_utf16_units": len(
                    str(project.path("src/main.cpp")).encode("utf-16-le")
                )
                // 2,
            }

            def observe_children() -> None:
                for marker in server.markers():
                    owned = json.loads(marker.read_text(encoding="utf-8"))
                    if owned["pid"] not in [identity.pid for identity in identities]:
                        identity = WindowsIdentity(owned["pid"])
                        self.assertEqual(owned["start_token"], f"win:{identity.creation:016x}")
                        identities.append(identity)

            def diagnostics(source: str) -> dict[str, Any]:
                result = server.call_envelope(
                    "get_code_diagnostics", {"file_path": source, "timeout_seconds": 60}
                )
                data["calls"].append(result)
                observe_children()
                return result

            try:
                server.initialize()
                clean = diagnostics("src/main.cpp")
                project.write_text(
                    "src/main.cpp", prefix + "int main() { return undefined_symbol; }" + ending
                )
                defect = diagnostics("src/main.cpp")
                project.write_text("src/main.cpp", clean_text)
                restored = diagnostics("src/main.cpp")
                data["before_replacement"] = [identity.evidence() for identity in identities]
                project.write_text("src/empty.cpp", prefix + "int empty() { return 0; }" + ending)
                add_entry("src/empty.cpp")
                replacement = diagnostics("src/empty.cpp")
                data["after_replacement"] = [identity.evidence() for identity in identities]
                data["markers_before_eof"] = [
                    json.loads(m.read_text(encoding="utf-8")) for m in server.markers()
                ]
                code, elapsed = server.close_stdin_and_wait(timeout=15)
                data.update(
                    eof_exit=code,
                    eof_seconds=elapsed,
                    after_eof=[identity.evidence() for identity in identities],
                    markers_after_eof=list(map(str, server.markers())),
                    stderr=server.stderr_text(),
                )
                for result in (clean, defect, restored, replacement):
                    self.assertEqual(result["status"], "ok", result)
                self.assertEqual(clean["data"]["diagnostics"], [])
                self.assertTrue(
                    any("undefined_symbol" in d["message"] for d in defect["data"]["diagnostics"])
                )
                self.assertEqual(restored["data"]["diagnostics"], [])
                self.assertEqual(replacement["data"]["diagnostics"], [])
                self.assertNotEqual(
                    restored["compilation_database_sha256"],
                    replacement["compilation_database_sha256"],
                )
                self.assertEqual(len(identities), 2, data)
                self.assertFalse(data["after_replacement"][0]["alive"], data)
                self.assertTrue(data["after_replacement"][1]["alive"], data)
                self.assertEqual(code, 0, data)
                self.assertLess(elapsed, 8.5, data)
                self.assertFalse(any(item["alive"] for item in data["after_eof"]), data)
                self.assertEqual(server.markers(), [])
                self.assertLess(data["source_path_utf16_units"], 260, data)
                for line in server.stdout_lines:
                    self.assertEqual(json.loads(line)["jsonrpc"], "2.0")
            finally:
                server.kill()
                for pipe in (server.proc.stdout, server.proc.stderr):
                    if pipe is not None:
                        pipe.close()
                data["emergency_cleanup"] = [identity.close() for identity in identities]
                record("replacement-unicode" if prefix else "replacement", data)


@unittest.skipUnless(os.name == "nt", "Windows cleanup failure and recovery")
class WindowsCleanupRecovery(unittest.TestCase):
    def test_unconfirmed_replacement_retains_identity_and_marker_then_recovers(self) -> None:
        with temporary_directory("byo-recovery-") as base:
            project = simple_project(base / "project")
            fake = FakeClangd(base / "peer")
            factory = fake_adapter_factory(fake)
            store = processes.ProcessMarkerStore(base / "markers")
            service = CodeAnalysisService(
                make_context(project.root),
                lambda program, snapshot, marker_store: factory(program, snapshot, store),
                program_resolver=fixed_resolver(Path(__file__)),
            )
            identities: list[WindowsIdentity] = []
            data: dict[str, Any] = {}
            try:
                first = service.diagnostics("src/main.c", timeout_seconds=10)
                self.assertEqual(first["status"], "ok", first)
                adapter = service._adapter
                assert adapter is not None and adapter.process is not None and adapter.marker
                marker = adapter.marker
                identities.append(WindowsIdentity(adapter.process.pid))
                project.write_text("src/empty.c", "int empty;\n")
                project.write_database([project.entry("src/main.c"), project.entry("src/empty.c")])
                with patch(
                    "pyocd_debug_mcp.adapters.clangd.terminate_process_group", return_value=False
                ):
                    failure = service.diagnostics("src/empty.c", timeout_seconds=10)
                data.update(failure=failure, retained_marker=json.loads(marker.read_text()))
                self.assertEqual(failure["code"], "analysis/cleanup-failed", failure)
                self.assertEqual(failure["status"], "error", failure)
                self.assertIs(service._adapter, adapter)
                self.assertIsNotNone(adapter.process)
                self.assertEqual(
                    data["retained_marker"]["start_token"], f"win:{identities[0].creation:016x}"
                )
                recovered = service.diagnostics("src/empty.c", timeout_seconds=10)
                data["recovered"] = recovered
                self.assertEqual(recovered["status"], "ok", recovered)
                self.assertFalse(marker.exists())
                assert service._adapter is not None and service._adapter.process is not None
                identities.append(WindowsIdentity(service._adapter.process.pid))
                service.shutdown()
                data["after_shutdown"] = [identity.evidence() for identity in identities]
                self.assertFalse(any(item["alive"] for item in data["after_shutdown"]), data)
                self.assertEqual(list(store.root.glob("*.json")), [])
            finally:
                try:
                    service.shutdown()
                finally:
                    data["emergency_cleanup"] = [identity.close() for identity in identities]
                    record("recovery", data)

    def test_blocked_transport_stays_visible_and_recovery_marker_is_retained(self) -> None:
        with temporary_directory("byo-blocked-drain-") as base:
            project = simple_project(base / "project")
            fake = FakeClangd(base / "peer")
            factory = fake_adapter_factory(fake)
            store = processes.ProcessMarkerStore(base / "markers")
            release = threading.Event()
            adapter = factory(
                fixed_resolver(Path(__file__))(None, "clangd", 0),
                load_analysis_config(project.root),
                store,
            )
            original_drain = adapter._drain_stderr

            def blocked_drain() -> None:
                release.wait()
                original_drain()

            adapter._drain_stderr = blocked_drain
            identity = None
            data: dict[str, Any] = {}
            try:
                adapter.start(time.monotonic() + 10)
                assert adapter.process is not None and adapter.marker is not None
                identity = WindowsIdentity(adapter.process.pid)
                marker = adapter.marker
                started = time.monotonic()
                with self.assertRaises(ClangdFailure) as raised:
                    adapter.shutdown(started + 0.2, graceful=False)
                data.update(
                    code=raised.exception.code,
                    message=str(raised.exception),
                    seconds=time.monotonic() - started,
                    after_native_cleanup=identity.evidence(),
                    marker_retained=marker.exists(),
                )
                self.assertEqual(raised.exception.code, "analysis/cleanup-failed")
                self.assertIn("transport still blocked", str(raised.exception))
                self.assertLess(data["seconds"], 1.5, data)
                self.assertFalse(data["after_native_cleanup"]["alive"], data)
                self.assertTrue(marker.exists())
                release.set()
                adapter.shutdown(time.monotonic() + 2)
                self.assertFalse(marker.exists())
                self.assertFalse(any(thread.is_alive() for thread in adapter._threads))
            finally:
                release.set()
                try:
                    adapter.shutdown(time.monotonic() + 2)
                finally:
                    data["emergency_cleanup"] = [] if identity is None else [identity.close()]
                    record("blocked-transport", data)


if __name__ == "__main__":
    unittest.main()
