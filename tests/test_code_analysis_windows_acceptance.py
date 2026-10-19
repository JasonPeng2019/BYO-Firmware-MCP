"""Independent Windows acceptance contracts for the clangd code-analysis server.

These tests are designed from the public contract rather than the owner's
regressions: a real ARM query driver must change what clangd can see, a
database edit alone must change diagnostics, live generations are tracked by
held native process handles, and every product cleanup claim is checked
before any test-owned emergency cleanup runs (which is recorded separately).
"""

from __future__ import annotations

import contextlib
import ctypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from ctypes import wintypes
from pathlib import Path
from typing import Any, Iterator

from pyocd_debug_mcp.adapters.clangd import ClangdAdapter, ClangdFailure
from pyocd_debug_mcp.code_analysis_config import load_analysis_config
from pyocd_debug_mcp.kernel.operations import ARGUMENT_TIMEOUT_GRACE_SECONDS
from pyocd_debug_mcp.kernel.processes import ProcessMarkerStore
from pyocd_debug_mcp.services.code_analysis import CodeAnalysisService
from tests.code_analysis_support import (
    AnalysisProject,
    AnalysisStdioServer,
    arm_gnu_root_or_skip,
    fixed_resolver,
    make_context,
    make_program,
    real_clangd_or_skip,
    sha256_file,
    simple_project,
)

FIXTURES = Path(__file__).resolve().parent / "code_analysis_fixtures" / "windows-acceptance"
UNREAD_STDIN_PEER = FIXTURES / "unread_stdin_peer.py"
EVIDENCE_ENV = "BYO_WINDOWS_ACCEPTANCE_EVIDENCE"
SCRATCH_ENV = "BYO_WINDOWS_ACCEPTANCE_SCRATCH"
MAX_PATH_UNITS = 260


def utf16_units(value: Path | str) -> int:
    return len(str(value).encode("utf-16-le")) // 2


def record(name: str, data: dict[str, Any]) -> None:
    if directory := os.environ.get(EVIDENCE_ENV):
        target = Path(directory)
        target.mkdir(parents=True, exist_ok=True)
        (target / (name + ".json")).write_text(
            json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
        )


@contextlib.contextmanager
def scratch(prefix: str) -> Iterator[Path]:
    """A short test-owned root (default C:/t) so source paths stay below MAX_PATH."""
    parent = os.environ.get(SCRATCH_ENV) or ("C:\\t" if Path("C:\\t").is_dir() else None)
    directory = Path(tempfile.mkdtemp(prefix=prefix, dir=parent)).resolve()
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def extended(path: Path) -> Path:
    return Path("\\\\?\\" + str(path))


class HeldProcess:
    """A native handle held from first observation, so PID reuse cannot mask survival."""

    _SYNCHRONIZE = 0x00100000
    _QUERY_LIMITED = 0x1000
    _TERMINATE = 0x0001

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self._k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        self._k32.OpenProcess.restype = wintypes.HANDLE
        self._k32.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (
            ctypes.POINTER(wintypes.FILETIME),
        ) * 4
        self._k32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        self._k32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        self._k32.WaitForSingleObject.restype = wintypes.DWORD
        self._k32.QueryFullProcessImageNameW.argtypes = (
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        )
        self._k32.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
        self._k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        self.handle = self._k32.OpenProcess(
            self._SYNCHRONIZE | self._QUERY_LIMITED | self._TERMINATE, False, pid
        )
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        times = [wintypes.FILETIME() for _ in range(4)]
        if not self._k32.GetProcessTimes(self.handle, *(ctypes.byref(t) for t in times)):
            raise ctypes.WinError(ctypes.get_last_error())
        self.creation = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        buffer = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buffer))
        if not self._k32.QueryFullProcessImageNameW(self.handle, 0, buffer, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        self.image = buffer.value
        self.emergency_terminated = False
        self.fault_injected: int | None = None

    def crash(self, code: int) -> None:
        """Fault injection: the child dies on its own, outside any product cleanup."""
        self.fault_injected = code
        if not self._k32.TerminateProcess(self.handle, code):
            raise ctypes.WinError(ctypes.get_last_error())
        if self.alive(5):
            raise AssertionError("injected crash was not confirmed")

    @property
    def key(self) -> tuple[int, int]:
        return self.pid, self.creation

    def alive(self, wait_seconds: float = 0) -> bool:
        result = self._k32.WaitForSingleObject(self.handle, int(wait_seconds * 1000))
        if result not in (0, 258):
            raise ctypes.WinError(ctypes.get_last_error())
        return result == 258

    def exit_code(self) -> int | None:
        code = wintypes.DWORD()
        if not self._k32.GetExitCodeProcess(self.handle, ctypes.byref(code)):
            raise ctypes.WinError(ctypes.get_last_error())
        return None if code.value == 259 else code.value

    def evidence(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "creation_filetime": self.creation,
            "image": self.image,
            "alive": self.alive(),
            "exit_code": self.exit_code(),
        }

    def release(self) -> dict[str, Any]:
        """Close the handle; terminate first only as emergency test cleanup."""
        observed = self.evidence()
        if observed["alive"]:
            self.emergency_terminated = True
            self._k32.TerminateProcess(self.handle, 97)
            observed["emergency_wait_confirmed"] = not self.alive(5)
        self._k32.CloseHandle(self.handle)
        observed["emergency_terminated"] = self.emergency_terminated
        return observed


def thread_snapshot(adapter: ClangdAdapter) -> dict[str, bool]:
    return {thread.name: thread.is_alive() for thread in adapter._threads}


def wait_until(predicate: Any, timeout: float, interval: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


def peer_clangd(executable: Path) -> subprocess.Popen[bytes]:
    """A test-owned clangd with the same image the product launches, never product-owned."""
    return subprocess.Popen(  # noqa: S603 - pinned fixture executable
        [str(executable)],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def retire_peer(peer: subprocess.Popen[bytes]) -> dict[str, Any]:
    """Peer fixture retirement: close its stdin (EOF) and confirm its exit."""
    observed: dict[str, Any] = {"pid": peer.pid, "alive_before_retire": peer.poll() is None}
    if peer.stdin is not None:
        with contextlib.suppress(OSError):
            peer.stdin.close()
    try:
        observed["exit_code"] = peer.wait(timeout=10)
        observed["forced"] = False
    except subprocess.TimeoutExpired:
        peer.kill()
        observed["exit_code"] = peer.wait(timeout=10)
        observed["forced"] = True
    return observed


# --------------------------------------------------------------- query driver
@unittest.skipUnless(os.name == "nt", "Windows extended-root query-driver parity")
class QueryDriverParity(unittest.TestCase):
    def test_relative_spellings_reach_clangd_argv_identically_under_extended_root(self) -> None:
        arm = arm_gnu_root_or_skip(self)
        compiler = arm / "bin" / "arm-none-eabi-g++.exe"
        with scratch("sa-qd-") as base:
            project = AnalysisProject(base / "p \u00e9")
            project.write_text("src/main.cpp", "int value;\n")
            project.write_database(
                [{"directory": ".", "file": "src/main.cpp", "arguments": ["g++", "-c", "x"]}]
            )
            relative = os.path.relpath(compiler, project.root)
            spellings = [relative, relative.replace("\\", "/"), "./" + relative.replace("\\", "/")]
            program = make_program(Path(sys.executable))
            observations = []
            for spelling in spellings:
                with self.subTest(spelling=spelling):
                    project.write_config(
                        {"schema_version": 1, "clangd": {"query_driver_paths": [spelling]}}
                    )
                    plain = load_analysis_config(project.root)
                    ext = load_analysis_config(extended(project.root))
                    argv_plain = ClangdAdapter(program, plain)._argv()
                    argv_ext = ClangdAdapter(program, ext)._argv()
                    drivers = [a for a in argv_ext if a.startswith("--query-driver=")]
                    observations.append(
                        {
                            "spelling": spelling,
                            "plain_drivers": list(map(str, plain.query_driver_paths)),
                            "extended_drivers": list(map(str, ext.query_driver_paths)),
                            "extended_query_driver_arg": drivers,
                            "plain_entries": [str(e.source) for e in plain.entries],
                            "extended_entries": [str(e.source) for e in ext.entries],
                            "problems": [p.to_payload() for p in ext.problems],
                        }
                    )
                    self.assertEqual(plain.problems, [], observations[-1])
                    self.assertEqual(ext.problems, [], observations[-1])
                    self.assertEqual(len(ext.query_driver_paths), 1)
                    self.assertTrue(ext.query_driver_paths[0].samefile(compiler))
                    self.assertEqual(
                        [a for a in argv_plain if a.startswith("--query-driver=")], drivers
                    )
                    self.assertEqual(
                        drivers, ["--query-driver=" + str(compiler).replace("\\", "/")]
                    )
                    # The relative database file resolves to the same source under both roots.
                    self.assertEqual(len(ext.entries), 1)
                    self.assertTrue(ext.entries[0].source.samefile(project.path("src/main.cpp")))
                    self.assertTrue(plain.entries[0].source.samefile(ext.entries[0].source))
            record("qd-parity", {"compiler": str(compiler), "cases": observations})

    def test_junction_alias_driver_is_one_literal_executable_under_both_roots(self) -> None:
        # Directory junctions need no symlink privilege, so reparse-point
        # aliasing stays covered where file symlink creation is unavailable.
        compiler = arm_gnu_root_or_skip(self) / "bin" / "arm-none-eabi-g++.exe"
        with scratch("sa-qj-") as base:
            project = AnalysisProject(base / "p")
            project.write_text("src/main.cpp", "int value;\n")
            project.write_database([project.entry("src/main.cpp")])
            junction = project.path("bin-alias")
            created = subprocess.run(  # noqa: S603 - fixed argv
                ["cmd", "/c", "mklink", "/J", str(junction), str(compiler.parent)],
                capture_output=True,
                timeout=30,
            )
            self.assertEqual(created.returncode, 0, created)
            project.write_config(
                {
                    "schema_version": 1,
                    "clangd": {"query_driver_paths": ["bin-alias/arm-none-eabi-g++.exe"]},
                }
            )
            snapshots = [
                load_analysis_config(root) for root in (project.root, extended(project.root))
            ]
            observed = [
                {
                    "drivers": list(map(str, s.query_driver_paths)),
                    "problems": [p.to_payload() for p in s.problems],
                }
                for s in snapshots
            ]
            record("qd-junction", {"junction": str(junction), "observed": observed})
            for snapshot in snapshots:
                self.assertEqual(snapshot.problems, [], observed)
                self.assertEqual(len(snapshot.query_driver_paths), 1, observed)
                driver = snapshot.query_driver_paths[0]
                self.assertTrue(driver.samefile(compiler), observed)
                self.assertFalse(str(driver).startswith("\\\\"), observed)
                self.assertFalse(any(c in str(driver) for c in "*?[],{}"), observed)
            self.assertEqual(observed[0]["drivers"], observed[1]["drivers"])
            os.rmdir(junction)  # remove the junction itself, never the toolchain

    def test_raw_patterns_uris_network_missing_and_nonexecutable_are_refused(self) -> None:
        arm = arm_gnu_root_or_skip(self)
        compiler = arm / "bin" / "arm-none-eabi-g++.exe"
        with scratch("sa-qr-") as base:
            project = AnalysisProject(base / "p")
            project.write_text("src/main.cpp", "int value;\n")
            project.write_database([project.entry("src/main.cpp")])
            # A genuine native image whose literal directory name holds a comma:
            # clangd's comma-separated glob list cannot represent it.
            comma_dir = project.path("tools,v1")
            comma_dir.mkdir(parents=True)
            shutil.copyfile(compiler, comma_dir / "cc.exe")
            braces_dir = project.path("tools{a}")
            braces_dir.mkdir()
            shutil.copyfile(compiler, braces_dir / "cc.exe")
            project.write_text("tools/script.BAT", "@echo off\r\n")
            project.write_text("tools/text.exe", "plain text named like an image\n")
            project.write_bytes("tools/mz-only.exe", b"MZ" + b"\0" * 62)
            project.write_bytes("tools/empty.exe", b"")
            project.path("tools/dir.exe").mkdir()
            ordinary = str(compiler)
            drive_less = ordinary[2:]
            cases = [
                "tools,v1/cc.exe",
                "tools{a}/cc.exe",
                "tools/cc.exe,tools,v1/cc.exe",
                "tools/[x]/../text.exe",
                "tools/?/../mz-only.exe",
                "tools/**/cc.exe",
                "tools/script.BAT",
                "tools/text.exe",
                "tools/mz-only.exe",
                "tools/empty.exe",
                "tools/dir.exe",
                "tools/absent.exe",
                str(base / "absent" / "cc.exe"),
                "tools/text.exe:stream",
                compiler.as_uri(),
                "file:" + ordinary,
                "https://example.invalid/arm-none-eabi-g++.exe",
                "\\\\localhost\\C$" + drive_less,
                "//localhost/C$" + drive_less.replace("\\", "/"),
                "\\\\?\\UNC\\localhost\\C$" + drive_less,
                "\\\\.\\" + ordinary,
            ]
            observations = []
            for root in (project.root, extended(project.root)):
                for value in cases:
                    with self.subTest(root=str(root), value=value):
                        project.write_config(
                            {"schema_version": 1, "clangd": {"query_driver_paths": [value]}}
                        )
                        snapshot = load_analysis_config(root)
                        observations.append(
                            {
                                "root": str(root),
                                "input": value,
                                "drivers": list(map(str, snapshot.query_driver_paths)),
                                "problems": [p.to_payload() for p in snapshot.problems],
                            }
                        )
                        self.assertEqual(snapshot.query_driver_paths, (), observations[-1])
                        self.assertTrue(snapshot.problems, observations[-1])
            # Control: the same genuine compiler copied to a literal path is accepted.
            literal = project.path("tools/cc.exe")
            shutil.copyfile(compiler, literal)
            project.write_config(
                {"schema_version": 1, "clangd": {"query_driver_paths": ["tools/cc.exe"]}}
            )
            accepted = load_analysis_config(extended(project.root))
            record(
                "qd-refusals", {"cases": observations, "control": str(accepted.query_driver_paths)}
            )
            self.assertEqual(accepted.problems, [])
            self.assertTrue(accepted.query_driver_paths[0].samefile(literal))


# ------------------------------------------------------- real clangd service
ARM_SOURCE = (
    "#include <newlib.h>\n"
    "#include <cstdint>\n"
    'static_assert(sizeof(void *) == 4, "ARM32 target supplied by the query driver");\n'
    'static_assert(sizeof(_NEWLIB_VERSION) > 1, "newlib headers supplied by the query driver");\n'
    "#if MODE == 2\n"
    "#error acceptance-database-mode-two\n"
    "#endif\n"
    "int main() { std::uint8_t zero = 0; return zero; }\n"
)
ARM_DEFECT = ARM_SOURCE.replace("return zero;", "return acceptance_undefined_symbol;")


@unittest.skipUnless(os.name == "nt", "Windows real clangd generation identity")
class RealClangdExtendedRoot(unittest.TestCase):
    def test_saved_diagnostics_and_truthful_database_replacement_under_extended_root(
        self,
    ) -> None:
        executable = real_clangd_or_skip(self)
        compiler = arm_gnu_root_or_skip(self) / "bin" / "arm-none-eabi-g++.exe"
        with scratch("sa-rc-") as base:
            project = AnalysisProject(base / "p")
            source = project.write_text("src/main.cpp", ARM_SOURCE)
            project.write_text("src/other.cpp", "int other() { return 1; }\n")
            project.write_config(
                {
                    "schema_version": 1,
                    "clangd": {"query_driver_paths": [os.path.relpath(compiler, project.root)]},
                }
            )
            store = ProcessMarkerStore(base / "markers")

            def flags(mode: int) -> list[str]:
                return [str(compiler), "-mcpu=cortex-m4", "-mthumb", "-std=c++17", f"-DMODE={mode}"]

            def write_database(mode: int) -> None:
                project.write_database(
                    [
                        {
                            "directory": str(project.root),
                            "file": name,
                            "arguments": [*flags(mode), "-c", name, "-o", str(base / "o.o")],
                        }
                        for name in ("src/main.cpp", "src/other.cpp")
                    ]
                )

            def compiler_verdict(mode: int) -> int:
                argv = [*flags(mode), "-fsyntax-only", "src/main.cpp"]
                result = subprocess.run(  # noqa: S603 - pinned fixture compiler
                    argv,
                    cwd=project.root,
                    capture_output=True,
                    timeout=60,
                )
                data.setdefault("compiler_verdicts", []).append(
                    {
                        "argv": argv,
                        "cwd": str(project.root),
                        "raw_exit": result.returncode,
                        "stdout": result.stdout.decode("utf-8", "replace"),
                        "stderr": result.stderr.decode("utf-8", "replace"),
                    }
                )
                return result.returncode

            adapters: list[ClangdAdapter] = []

            def factory(program: Any, snapshot: Any, marker_store: Any) -> ClangdAdapter:
                adapter = ClangdAdapter(program, snapshot, store)
                adapters.append(adapter)
                return adapter

            write_database(1)
            service = CodeAnalysisService(
                make_context(extended(project.root)),
                factory,
                program_resolver=fixed_resolver(executable, version=None),
            )
            held: dict[tuple[int, int], HeldProcess] = {}
            data: dict[str, Any] = {
                "clangd": str(executable),
                "clangd_sha256": sha256_file(executable),
                "compiler": str(compiler),
                "compiler_sha256": sha256_file(compiler),
                "source_utf16_units": utf16_units(source),
                "steps": [],
            }
            peer = None
            peer_identity: HeldProcess | None = None

            def generation() -> HeldProcess:
                adapter = service._adapter
                assert adapter is not None and adapter.process is not None
                identity = HeldProcess(adapter.process.pid)
                identity = held.setdefault(identity.key, identity)
                marker = json.loads(Path(adapter.marker or "").read_text(encoding="utf-8"))
                self.assertEqual(marker["pid"], identity.pid)
                self.assertEqual(marker["start_token"], f"win:{identity.creation:016x}")
                self.assertTrue(Path(identity.image).samefile(executable))
                return identity

            def diagnose(label: str, path: str = "src/main.cpp") -> tuple[dict, HeldProcess]:
                result = service.diagnostics(path, timeout_seconds=90)
                identity = generation()
                data["steps"].append(
                    {"label": label, "result": result, "generation": list(identity.key)}
                )
                self.assertEqual(result["status"], "ok", result)
                return result, identity

            def messages(result: dict) -> list[str]:
                return [d["message"] for d in result["data"]["diagnostics"]]

            try:
                self.assertLess(utf16_units(source), MAX_PATH_UNITS)
                self.assertEqual(compiler_verdict(1), 0, data)
                clean, first = diagnose("clean")
                self.assertEqual(messages(clean), [], clean)

                project.write_text("src/main.cpp", ARM_DEFECT)
                defect, same = diagnose("defect")
                self.assertTrue(
                    any("acceptance_undefined_symbol" in m for m in messages(defect)), defect
                )
                project.write_text("src/main.cpp", ARM_SOURCE)
                restored, again = diagnose("restored")
                self.assertEqual(messages(restored), [], restored)
                self.assertEqual({first.key, same.key, again.key}, {first.key})

                # Concurrency within one generation: two saved files at once.
                results: dict[str, Any] = {}

                def worker(name: str) -> None:
                    results[name] = service.diagnostics(name, timeout_seconds=90)

                threads = [
                    threading.Thread(target=worker, args=(name,))
                    for name in ("src/main.cpp", "src/other.cpp")
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(120)
                data["concurrent"] = results
                self.assertEqual(sorted(results), ["src/main.cpp", "src/other.cpp"])
                for result in results.values():
                    self.assertEqual(result["status"], "ok", result)
                    self.assertEqual(result["data"]["diagnostics"], [], result)
                self.assertEqual(generation().key, first.key)

                # A database edit alone (same source bytes) must yield a new truthful generation.
                peer = peer_clangd(executable)
                peer_identity = HeldProcess(peer.pid)
                write_database(2)
                self.assertNotEqual(compiler_verdict(2), 0)
                replaced, second = diagnose("database-mode-2")
                self.assertTrue(
                    # clangd capitalizes the first letter of a #error message.
                    any("acceptance-database-mode-two" in m.lower() for m in messages(replaced)),
                    replaced,
                )
                self.assertNotEqual(
                    restored["compilation_database_sha256"], replaced["compilation_database_sha256"]
                )
                self.assertNotEqual(second.key, first.key)
                self.assertFalse(first.alive(), first.evidence())
                self.assertTrue(second.alive(), second.evidence())

                write_database(1)
                back, third = diagnose("database-mode-1")
                self.assertEqual(messages(back), [], back)
                self.assertEqual(
                    back["compilation_database_sha256"], restored["compilation_database_sha256"]
                )
                self.assertNotIn(third.key, {first.key, second.key})
                self.assertFalse(second.alive(), second.evidence())
                self.assertEqual(peer_identity.image, third.image)
                retired = [a for a in adapters if a is not service._adapter]
                data["generations_before_shutdown"] = [h.evidence() for h in held.values()]

                service.shutdown()
                data.update(
                    after_shutdown=[h.evidence() for h in held.values()],
                    peer_after_shutdown=peer_identity.evidence(),
                    threads=[thread_snapshot(a) for a in adapters],
                    pending=[len(a._pending) for a in adapters],
                    markers=[str(m) for m in store.root.glob("*.json")],
                    retired_stderr_has_exit=[("<-- exit" in a.stderr_tail) for a in retired],
                )
                self.assertEqual(len(adapters), 3, data)
                self.assertFalse(any(h.alive() for h in held.values()), data)
                self.assertTrue(peer_identity.alive(), data)
                self.assertTrue(
                    all(not alive for threads in data["threads"] for alive in threads.values()),
                    data,
                )
                self.assertEqual(data["pending"], [0, 0, 0], data)
                self.assertEqual(data["markers"], [], data)
                # Responsive generations are retired through the LSP exit exchange.
                self.assertTrue(all(data["retired_stderr_has_exit"]), data)
            finally:
                try:
                    service.shutdown()
                finally:
                    data["emergency_cleanup"] = [h.release() for h in held.values()]
                    if peer is not None:
                        data["peer_fixture_retire"] = retire_peer(peer)
                    if peer_identity is not None:
                        peer_identity.release()
                    record("real-extended-replacement", data)
            self.assertFalse(any(h.emergency_terminated for h in held.values()), data)


class LockedFile:
    """Open a file without FILE_SHARE_DELETE so a real unlink fails until release."""

    def __init__(self, path: Path) -> None:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        )
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        self._close = k32.CloseHandle
        # GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE (no delete), OPEN_EXISTING.
        self.handle = k32.CreateFileW(str(path), 0x80000000, 0x3, None, 3, 0x80, None)
        if self.handle in (None, wintypes.HANDLE(-1).value):
            raise ctypes.WinError(ctypes.get_last_error())

    def release(self) -> None:
        if self.handle is not None:
            self._close(self.handle)
            self.handle = None


@unittest.skipUnless(os.name == "nt", "Windows real cleanup failure and recovery")
class RealCleanupFailureRecovery(unittest.TestCase):
    def test_unremovable_marker_failure_is_visible_then_recovers(self) -> None:
        executable = real_clangd_or_skip(self)
        with scratch("sa-cf-") as base:
            project = simple_project(base / "p")
            store = ProcessMarkerStore(base / "markers")
            service = CodeAnalysisService(
                make_context(project.root),
                lambda program, snapshot, marker_store: ClangdAdapter(program, snapshot, store),
                program_resolver=fixed_resolver(executable, version=None),
            )
            held: list[HeldProcess] = []
            lock = None
            data: dict[str, Any] = {}
            try:
                first = service.diagnostics("src/main.c", timeout_seconds=60)
                self.assertEqual(first["status"], "ok", first)
                adapter = service._adapter
                assert adapter is not None and adapter.process is not None
                marker = Path(adapter.marker or "")
                held.append(HeldProcess(adapter.process.pid))
                lock = LockedFile(marker)
                project.write_text("src/second.c", "int second(void) { return 2; }\n")
                project.write_database([project.entry("src/main.c"), project.entry("src/second.c")])
                failed = service.diagnostics("src/second.c", timeout_seconds=60)
                data.update(
                    failed=failed,
                    old_after_failure=held[0].evidence(),
                    marker_retained=marker.exists(),
                    status_after_failure=service.status(),
                )
                # The child is natively gone, but unconfirmed bookkeeping is not success.
                self.assertNotEqual(failed["status"], "ok", data)
                # A native unlink failure is a cleanup failure, not a protocol one.
                self.assertEqual(failed["code"], "analysis/cleanup-failed", data)
                self.assertIn("WinError 32", failed["message"], data)
                self.assertFalse(held[0].alive(), data)
                self.assertTrue(marker.exists(), data)
                self.assertEqual(
                    sorted(p.name for p in store.root.glob("*.json")), [marker.name], data
                )
                lock.release()
                recovered = service.diagnostics("src/second.c", timeout_seconds=60)
                data["recovered"] = recovered
                self.assertEqual(recovered["status"], "ok", data)
                self.assertFalse(marker.exists(), data)
                assert service._adapter is not None and service._adapter.process is not None
                held.append(HeldProcess(service._adapter.process.pid))
                self.assertNotEqual(held[0].key, held[1].key)
                service.shutdown()
                data["after_shutdown"] = [h.evidence() for h in held]
                data["markers_after_shutdown"] = [str(p) for p in store.root.glob("*.json")]
                self.assertFalse(any(h.alive() for h in held), data)
                self.assertEqual(data["markers_after_shutdown"], [], data)
            finally:
                if lock is not None:
                    lock.release()
                try:
                    service.shutdown()
                finally:
                    data["emergency_cleanup"] = [h.release() for h in held]
                    record("real-cleanup-failure-recovery", data)
            self.assertFalse(any(h.emergency_terminated for h in held), data)


# ---------------------------------------------------- real clangd over stdio
UNICODE_LINES = [
    "/* \U0001f600\U0001d11e \u00b5 */ static int twice(int v) { return v * 2; }",
    "/* \u6d4b\u8bd5\U0001f600 */ int use() { return twice(3) + twice(4); }",
    "int main() { return use(); }",
]


@unittest.skipUnless(os.name == "nt", "Windows real clangd stdio queries and EOF")
class RealClangdStdio(unittest.TestCase):
    def test_unicode_non_bmp_crlf_queries_then_clean_eof_spares_same_image_peer(self) -> None:
        executable = real_clangd_or_skip(self)
        compiler = arm_gnu_root_or_skip(self) / "bin" / "arm-none-eabi-g++.exe"
        with scratch("sa-io-") as base:
            project = AnalysisProject(base / "\u00fc\u6d4b\U0001f600")
            text = "\r\n".join(UNICODE_LINES) + "\r\n"
            source = project.write_text("src/main.cpp", text)
            self.assertEqual(source.read_bytes().count(b"\r\n"), 3)
            project.write_config(
                {"schema_version": 1, "clangd": {"query_driver_paths": [str(compiler)]}}
            )
            project.write_database(
                [
                    {
                        "directory": str(project.root),
                        "file": "src/main.cpp",
                        "arguments": [str(compiler), "-std=c++17", "-c", "src/main.cpp"],
                    }
                ]
            )
            server = AnalysisStdioServer(base / "srv", project.root, executable=executable)
            held: dict[tuple[int, int], HeldProcess] = {}
            data: dict[str, Any] = {
                "clangd_sha256": sha256_file(executable),
                "source_utf16_units": utf16_units(source),
                "calls": {},
            }
            peer = None
            peer_identity = None
            use_column = UNICODE_LINES[1].index("twice") + 1
            second_use_column = UNICODE_LINES[1].rindex("twice") + 1
            definition_column = UNICODE_LINES[0].index("twice") + 1

            def call(name: str, **arguments: Any) -> dict[str, Any]:
                result = server.call_envelope(name, arguments)
                data["calls"].setdefault(name, []).append(result)
                return result

            def starts(locations: list[dict]) -> list[tuple[int, int]]:
                return sorted(
                    (item["range"]["start"]["line"], item["range"]["start"]["column"])
                    for item in locations
                )

            try:
                self.assertLess(utf16_units(source), MAX_PATH_UNITS)
                server.initialize()
                status = call("get_code_analysis_status")
                self.assertIsInstance(status, dict)
                self.assertEqual(server.markers(), [], "status must not start clangd")

                diagnostics = call("get_code_diagnostics", file_path="src/main.cpp")
                self.assertEqual(diagnostics["status"], "ok", diagnostics)
                self.assertEqual(diagnostics["data"]["diagnostics"], [], diagnostics)
                for pid in server.descendants():
                    with contextlib.suppress(OSError):
                        identity = HeldProcess(pid)
                        held.setdefault(identity.key, identity)
                owned = [json.loads(m.read_text(encoding="utf-8")) for m in server.markers()]
                data["markers_live"] = owned
                self.assertEqual(len(owned), 1, owned)
                clangd_children = [h for h in held.values() if h.pid == owned[0]["pid"]]
                self.assertEqual(len(clangd_children), 1, [h.evidence() for h in held.values()])
                self.assertEqual(owned[0]["start_token"], f"win:{clangd_children[0].creation:016x}")

                definition = call(
                    "find_code_definition", file_path="src/main.cpp", line=2, column=use_column
                )
                self.assertEqual(definition["status"], "ok", definition)
                self.assertEqual(
                    starts(definition["data"]["locations"]), [(1, definition_column)], definition
                )
                self.assertTrue(
                    Path(definition["data"]["locations"][0]["path"]).samefile(source), definition
                )

                references = call(
                    "find_code_references",
                    file_path="src/main.cpp",
                    line=1,
                    column=definition_column,
                    include_declaration=True,
                )
                self.assertEqual(references["status"], "ok", references)
                self.assertEqual(
                    starts(references["data"]["locations"]),
                    [(1, definition_column), (2, use_column), (2, second_use_column)],
                    references,
                )

                hover = call("get_code_hover", file_path="src/main.cpp", line=2, column=use_column)
                self.assertEqual(hover["status"], "ok", hover)
                self.assertIn("twice", json.dumps(hover["data"], ensure_ascii=False))

                # Splitting a non-BMP character is impossible in code points; a column
                # past the end of the line is a strict argument refusal, not a crash.
                beyond = call(
                    "find_code_definition",
                    file_path="src/main.cpp",
                    line=1,
                    column=len(UNICODE_LINES[0]) + 5,
                )
                self.assertNotEqual(beyond["status"], "ok", beyond)

                peer = peer_clangd(executable)
                peer_identity = HeldProcess(peer.pid)
                self.assertEqual(peer_identity.image, clangd_children[0].image)
                data["before_eof"] = [h.evidence() for h in held.values()]
                code, seconds = server.close_stdin_and_wait(timeout=20)
                data.update(
                    eof_exit=code,
                    eof_seconds=seconds,
                    after_eof=[h.evidence() for h in held.values()],
                    markers_after_eof=[str(m) for m in server.markers()],
                    peer_after_eof=peer_identity.evidence(),
                    stderr_tail=server.stderr_text()[-4000:],
                )
                self.assertEqual(code, 0, data)
                self.assertLess(seconds, 8.5, data)
                self.assertFalse(any(h.alive() for h in held.values()), data)
                self.assertEqual(data["markers_after_eof"], [], data)
                self.assertTrue(peer_identity.alive(), data)
                for line in server.stdout_lines:
                    if line.strip():
                        self.assertEqual(json.loads(line)["jsonrpc"], "2.0")
            finally:
                server.kill()
                for pipe in (server.proc.stdout, server.proc.stderr):
                    if pipe is not None:
                        pipe.close()
                data["emergency_cleanup"] = [h.release() for h in held.values()]
                if peer is not None:
                    data["peer_fixture_retire"] = retire_peer(peer)
                if peer_identity is not None:
                    peer_identity.release()
                record("real-stdio-unicode-eof", data)
            self.assertFalse(any(h.emergency_terminated for h in held.values()), data)


# ------------------------------------------------- unread stdin cancellation
class UnreadStdinAdapter(ClangdAdapter):
    ready_file: Path

    def _argv(self) -> list[str]:
        return [sys.executable, str(UNREAD_STDIN_PEER), str(self.ready_file)]


@unittest.skipUnless(os.name == "nt", "Windows synchronous pipe writes")
class UnreadStdinCancellation(unittest.TestCase):
    def start_blocked(self, base: Path) -> tuple[UnreadStdinAdapter, HeldProcess, Path]:
        project = simple_project(base / "p")
        adapter = UnreadStdinAdapter(
            make_program(Path(sys.executable)),
            load_analysis_config(project.root),
            ProcessMarkerStore(base / "markers"),
        )
        adapter.ready_file = base / "ready.txt"
        adapter.start(time.monotonic() + 20)
        self.addCleanup(lambda: adapter.shutdown(time.monotonic() + 5))
        assert adapter.process is not None and adapter.marker is not None
        self.assertTrue(wait_until(adapter.ready_file.exists, 10), "peer never became ready")
        identity = HeldProcess(adapter.process.pid)
        # 32 MiB cannot fit the pipe; a sentinel behind it proves the writer is
        # stuck inside the write, not idle.
        adapter.notify("acceptance/fill", {"blob": "x" * (32 << 20)})
        adapter.notify("acceptance/sentinel", {})
        self.assertFalse(wait_until(lambda: adapter._outgoing.qsize() == 0, 0.4))
        self.assertTrue(thread_snapshot(adapter)["clangd-writer"])
        return adapter, identity, adapter.marker

    def run_blocked(self, label: str, *, deadline_after: float, cancel_after: float | None) -> None:
        with scratch("sa-us-") as base:
            adapter, identity, marker = self.start_blocked(base)
            outcomes: dict[str, Any] = {}
            cancel = threading.Event()

            def call(name: str, deadline: float, cancellation: Any = None) -> None:
                try:
                    adapter.request("acceptance/" + name, {}, deadline, cancellation)
                    outcomes[name] = ("returned", time.monotonic())
                except BaseException as exc:  # noqa: BLE001 - outcome is the assertion
                    outcomes[name] = (type(exc).__name__, time.monotonic(), str(exc))

            data: dict[str, Any] = {"label": label}
            try:
                started = time.monotonic()
                sibling = threading.Thread(target=call, args=("sibling", started + 60))
                sibling.start()
                expire = started + deadline_after
                primary = threading.Thread(
                    target=call,
                    args=("primary", expire, cancel if cancel_after is not None else None),
                )
                primary.start()
                if cancel_after is not None:
                    time.sleep(cancel_after)
                    cancel.set()
                    expire = time.monotonic()
                bound = ARGUMENT_TIMEOUT_GRACE_SECONDS + 1.0
                primary.join(bound + deadline_after)
                sibling.join(bound)
                data.update(
                    outcomes={k: [v[0], v[1] - started, *v[2:]] for k, v in outcomes.items()},
                    expire_offset=expire - started,
                    primary_alive=primary.is_alive(),
                    sibling_alive=sibling.is_alive(),
                    threads=thread_snapshot(adapter),
                    process=identity.evidence(),
                    marker_exists=marker.exists(),
                    adapter_process_cleared=adapter.process is None,
                    pending=len(adapter._pending),
                )
                self.assertFalse(data["primary_alive"], data)
                self.assertFalse(data["sibling_alive"], data)
                kind, primary_at = outcomes["primary"][:2]
                self.assertEqual(kind, "TimeoutError", data)
                # Not early: the primary expires at its own deadline/cancellation...
                self.assertGreaterEqual(primary_at, expire - 0.05, data)
                # ...and the blocked writer cannot extend it past the bounded grace.
                self.assertLess(primary_at - expire, bound, data)
                sibling_kind, sibling_at = outcomes["sibling"][:2]
                self.assertEqual(sibling_kind, "ClangdFailure", data)
                self.assertLess(sibling_at - expire, bound, data)
                self.assertFalse(any(data["threads"].values()), data)
                self.assertFalse(data["process"]["alive"], data)
                self.assertFalse(data["marker_exists"], data)
                self.assertTrue(data["adapter_process_cleared"], data)
                self.assertEqual(data["pending"], 0, data)
                with self.assertRaises(ClangdFailure):
                    adapter.request("acceptance/after", {}, time.monotonic() + 1)
            finally:
                data["emergency_cleanup"] = identity.release()
                record("unread-stdin-" + label, data)
            self.assertFalse(identity.emergency_terminated, data)

    def test_deadline_expiry_bounds_blocked_writer_and_releases_sibling(self) -> None:
        self.run_blocked("deadline", deadline_after=0.6, cancel_after=None)

    def test_cancellation_bounds_blocked_writer_and_releases_sibling(self) -> None:
        self.run_blocked("cancel", deadline_after=60, cancel_after=0.5)


# ------------------------------------------ reader/caller cleanup contention
class CallerOwnedGuard:
    """Delegating lock that reports when a non-reader thread owns shutdown."""

    def __init__(self, inner: Any, caller_owns: threading.Event) -> None:
        self._inner = inner
        self._caller_owns = caller_owns
        self.reader_attempts: list[dict[str, Any]] = []

    def acquire(self, *args: Any, **kwargs: Any) -> bool:
        reader = threading.current_thread().name == "clangd-reader"
        caller_owned = self._caller_owns.is_set()
        acquired = self._inner.acquire(*args, **kwargs)
        if reader:
            self.reader_attempts.append(
                {
                    "args": list(args),
                    "kwargs": kwargs,
                    "caller_owned": caller_owned,
                    "acquired": acquired,
                }
            )
        elif acquired:
            self._caller_owns.set()
        return acquired

    def release(self) -> None:
        self._inner.release()

    def __enter__(self) -> CallerOwnedGuard:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


class ContendedShutdown:
    """Holds the reader's own EOF cleanup until the caller owns shutdown.

    This forces the interleaving a spontaneous child exit can produce
    naturally: the reader has decided to clean up, and a caller shutdown that
    already owns cleanup must not wait on (or blame) that reader.
    """

    forced = False
    reader_entered: threading.Event
    caller_owns: threading.Event
    reader_boundary: str | None = None

    def _hold_reader(self, boundary: str) -> None:
        # The reader has committed to its own EOF cleanup but has not yet
        # touched the shutdown guard; release it only once a caller owns it.
        if (
            self.forced
            and threading.current_thread().name == "clangd-reader"
            and not self.reader_entered.is_set()
        ):
            self.reader_boundary = boundary
            self.reader_entered.set()
            self.caller_owns.wait(10)

    # The reader's cleanup entrypoint is private and may move; gate whichever
    # boundary it reaches first (public shutdown before, _shutdown after the
    # ownership correction) so the forced interleaving is never silently lost.
    def shutdown(self, deadline: float, **kwargs: Any) -> None:
        self._hold_reader("shutdown")
        super().shutdown(deadline, **kwargs)  # type: ignore[misc]

    def _shutdown(self, deadline: float, **kwargs: Any) -> None:
        self._hold_reader("_shutdown")
        super()._shutdown(deadline, **kwargs)  # type: ignore[misc]

    def force_contention(self) -> None:
        self.forced = True
        self.reader_entered = threading.Event()
        self.caller_owns = threading.Event()
        self.probe = CallerOwnedGuard(self._shutdown_guard, self.caller_owns)  # type: ignore[attr-defined]
        self._shutdown_guard = self.probe


class ContendedFakeAdapter(ContendedShutdown, UnreadStdinAdapter):
    pass


class ContendedRealAdapter(ContendedShutdown, ClangdAdapter):
    pass


@unittest.skipUnless(os.name == "nt", "Windows native process identity")
class ReaderCallerCleanupContention(unittest.TestCase):
    CALLER_DEADLINE = 8.0

    def start(self, base: Path, real: Path | None) -> tuple[Any, HeldProcess, Path]:
        project = simple_project(base / "p")
        store = ProcessMarkerStore(base / "markers")
        if real is None:
            adapter: Any = ContendedFakeAdapter(
                make_program(Path(sys.executable)), load_analysis_config(project.root), store
            )
            adapter.ready_file = base / "ready.txt"
        else:
            adapter = ContendedRealAdapter(
                make_program(real), load_analysis_config(project.root), store
            )
        adapter.start(time.monotonic() + 30)
        self.addCleanup(lambda: adapter.shutdown(time.monotonic() + 5))
        assert adapter.process is not None and adapter.marker is not None
        if real is None:
            self.assertTrue(wait_until(adapter.ready_file.exists, 10), "peer never became ready")
        return adapter, HeldProcess(adapter.process.pid), adapter.marker

    def crash_then_shutdown(self, base: Path, *, forced: bool, real: Path | None) -> dict:
        adapter, identity, marker = self.start(base, real)
        data: dict[str, Any] = {"forced": forced, "real": real is not None}
        try:
            data["identity"] = identity.evidence()
            if forced:
                adapter.force_contention()
            identity.crash(41)
            if forced:
                data["reader_entered_cleanup"] = adapter.reader_entered.wait(5)
            started = time.monotonic()
            try:
                adapter.shutdown(started + self.CALLER_DEADLINE)
                data["caller"] = ["returned", time.monotonic() - started]
            except ClangdFailure as exc:
                data["caller"] = [exc.code, time.monotonic() - started, str(exc)]
            data["threads_quiesced"] = wait_until(
                lambda: not any(thread_snapshot(adapter).values()), 10
            )
            started = time.monotonic()
            try:
                adapter.shutdown(started + 5)
                data["repeat"] = ["returned", time.monotonic() - started]
            except ClangdFailure as exc:
                data["repeat"] = [exc.code, time.monotonic() - started, str(exc)]
            data.update(
                threads=thread_snapshot(adapter),
                process=identity.evidence(),
                marker_exists=marker.exists(),
                markers_left=sorted(p.name for p in marker.parent.iterdir()),
                adapter_process_cleared=adapter.process is None,
                pending=len(adapter._pending),
            )
            if forced:
                data["caller_owned_guard"] = adapter.caller_owns.is_set()
                data["reader_boundary"] = adapter.reader_boundary
                data["reader_guard_attempts"] = adapter.probe.reader_attempts
        finally:
            data["emergency_cleanup"] = identity.release()
        return data

    def assert_forced(self, data: dict) -> None:
        # The interleaving really happened: the reader reached its cleanup
        # boundary first, and met the real guard only while the caller owned it.
        self.assertTrue(data["reader_entered_cleanup"], data)
        self.assertTrue(data["caller_owned_guard"], data)
        self.assertTrue(data["reader_guard_attempts"], data)
        self.assertTrue(data["reader_guard_attempts"][0]["caller_owned"], data)

    def assert_clean(self, data: dict) -> None:
        # A caller that owns cleanup after the child died must succeed promptly,
        # not wait out its deadline on the reader and report cleanup-failed.
        self.assertEqual(data["caller"][0], "returned", data)
        self.assertLess(data["caller"][1], self.CALLER_DEADLINE / 2, data)
        self.assertEqual(data["repeat"][0], "returned", data)
        self.assertTrue(data["threads_quiesced"], data)
        self.assertFalse(data["process"]["alive"], data)
        # The injected crash, not product termination, ended the child.
        self.assertEqual(data["process"]["exit_code"], 41, data)
        self.assertFalse(data["marker_exists"], data)
        self.assertEqual(data["markers_left"], [], data)
        self.assertTrue(data["adapter_process_cleared"], data)
        self.assertEqual(data["pending"], 0, data)
        self.assertFalse(data["emergency_cleanup"]["emergency_terminated"], data)

    def test_forced_reader_cleanup_behind_caller_owner_fake_peer(self) -> None:
        with scratch("sa-rc-") as base:
            data = self.crash_then_shutdown(base, forced=True, real=None)
            record("contention-forced-fake", data)
            self.assert_forced(data)
            self.assert_clean(data)

    def test_forced_reader_cleanup_behind_caller_owner_real_clangd(self) -> None:
        executable = real_clangd_or_skip(self)
        with scratch("sa-rr-") as base:
            data = self.crash_then_shutdown(base, forced=True, real=executable)
            record("contention-forced-real", data)
            self.assert_forced(data)
            self.assert_clean(data)

    def test_unforced_crash_and_immediate_shutdown_repeatedly(self) -> None:
        runs = []
        for index in range(12):
            with scratch(f"sa-ru{index}-") as base:
                runs.append(self.crash_then_shutdown(base, forced=False, real=None))
        record("contention-unforced-fake", {"runs": runs})
        for data in runs:
            self.assert_clean(data)


if __name__ == "__main__":
    unittest.main()
